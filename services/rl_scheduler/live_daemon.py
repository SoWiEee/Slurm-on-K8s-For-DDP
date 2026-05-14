"""Step 5: Live scheduling daemon — DSAC on the real Slurm cluster.

Polls squeue every POLL_INTERVAL seconds. When pending jobs exist and
resources are free, calls the DSACAgent to select (job, node, gpu) and
executes placement via srun with explicit --nodelist + --gres=mps:N.

Logs every (obs, act, mask, rew, next_obs, done) transition to a JSONL
file for later RLPD fine-tuning.

Safety invariants:
  - Never issues srun if live_buf < LIVE_WARMUP_MIN transitions (fallback only)
  - Abstains if agent value < VALUE_ABSTAIN or entropy > ENTROPY_ABSTAIN
  - SHADOW_MODE=true → logs decisions but never executes srun (default)

Usage::
    # Shadow mode (log only, no actual placement):
    SHADOW_MODE=true .venv-m11/bin/python -m services.rl_scheduler.live_daemon \\
        --policy-dir runs/dsac_sim \\
        --node-name slurm-worker-0 --gpu-hostname 192.168.1.10 \\
        --log-dir shadow_logs

    # Live mode (executes srun):
    SHADOW_MODE=false .venv-m11/bin/python -m services.rl_scheduler.live_daemon \\
        --policy-dir runs/dsac_sim \\
        --node-name slurm-worker-0 \\
        --log-dir live_logs --live-warmup 500
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from sim.gym_env import (
    GPU_FEAT_DIM, GPU_TYPES, GLOBAL_FEAT_DIM, JOB_FEAT_DIM,
    TOPO_FEAT_DIM, TOP_K, env_dims,
)
from sim.loader import MPS_PER_GPU
from services.rl_scheduler.dsac import DSACAgent
from services.rl_scheduler.rlpd_finetune import ReplayBuffer, Transition


# ── Cluster state from Slurm ──────────────────────────────────────────────

@dataclass
class LiveJob:
    job_id:    str
    mps_req:   int
    gpu_count: int
    gpu_type:  str
    runtime:   float   # predicted (from predictor API) or 0
    submit_ts: float
    state:     str     # PENDING / RUNNING / COMPLETED / etc.
    nodelist:  str     # empty if pending


@dataclass
class LiveGpu:
    node_name:    str
    gpu_index:    int
    free_mps:     int
    running_jobs: int = 0


@dataclass
class LiveCluster:
    nodes: Dict[str, List[LiveGpu]] = field(default_factory=dict)
    mps_per_gpu: int = MPS_PER_GPU


def _run(cmd: List[str], timeout: int = 10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _parse_squeue(raw: str) -> List[LiveJob]:
    """Parse squeue --json output into LiveJob list."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    jobs = []
    for j in data.get("jobs", []):
        state = j.get("job_state", ["UNKNOWN"])
        if isinstance(state, list):
            state = state[0]
        # Extract MPS from GRES string, e.g. "gpu:mps:4"
        tres = j.get("tres_req_str", "") or j.get("gres", "") or ""
        mps_req = 0
        for part in str(tres).split(","):
            if "mps" in part.lower():
                nums = [int(x) for x in part.split(":") if x.isdigit()]
                if nums:
                    mps_req = nums[-1]
        gpu_count = j.get("gpus_total", 1) or 1
        jobs.append(LiveJob(
            job_id=str(j.get("job_id", "")),
            mps_req=mps_req or MPS_PER_GPU,  # default = full GPU
            gpu_count=int(gpu_count),
            gpu_type="rtx4070",
            runtime=float(j.get("time_limit", {}).get("number", 0) * 60
                          if isinstance(j.get("time_limit"), dict) else 0),
            submit_ts=float(j.get("submit_time", {}).get("number", time.time())
                            if isinstance(j.get("submit_time"), dict) else time.time()),
            state=state,
            nodelist=str(j.get("nodes", "") or ""),
        ))
    return jobs


def _parse_scontrol_node(raw: str, node_name: str,
                          mps_per_gpu: int, n_gpus: int) -> List[LiveGpu]:
    """Parse scontrol show node output to extract free MPS per GPU."""
    gpus = []
    # Try to find AllocTRES/CfgTRES for MPS slots
    free_mps = mps_per_gpu  # default = fully free

    for line in raw.split("\n"):
        if "AllocTRES" in line:
            for token in line.split():
                if "mps" in token.lower() and "=" in token:
                    try:
                        used = int(token.split("mps")[-1].lstrip(":="))
                        free_mps = max(0, mps_per_gpu - used)
                    except ValueError:
                        pass

    for gi in range(n_gpus):
        gpus.append(LiveGpu(
            node_name=node_name, gpu_index=gi,
            free_mps=free_mps,  # simplified: same free for all GPUs on node
        ))
    return gpus


def query_cluster(node_names: List[str], n_gpus: int,
                  mps_per_gpu: int) -> LiveCluster:
    """Query Slurm for current GPU state on each node."""
    cluster = LiveCluster(mps_per_gpu=mps_per_gpu)
    for node in node_names:
        raw  = _run(["scontrol", "show", "node", node])
        gpus = _parse_scontrol_node(raw, node, mps_per_gpu, n_gpus)
        cluster.nodes[node] = gpus
    return cluster


def query_pending_jobs(runtime_predictor_url: Optional[str] = None) -> List[LiveJob]:
    """Get pending jobs from squeue, optionally call runtime predictor."""
    raw  = _run(["squeue", "--json"])
    jobs = _parse_squeue(raw)
    pending = [j for j in jobs if j.state == "PENDING"]

    if runtime_predictor_url:
        for j in pending:
            if j.runtime <= 0:
                try:
                    import urllib.request, urllib.parse
                    body = json.dumps({"gpu_count": j.gpu_count,
                                       "mps_req": j.mps_req}).encode()
                    req = urllib.request.Request(
                        runtime_predictor_url,
                        data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=1) as r:
                        pred = json.loads(r.read())
                        j.runtime = float(pred.get("predicted_s", 600))
                except Exception:
                    j.runtime = 600.0   # fallback: 10 min
    else:
        for j in pending:
            if j.runtime <= 0:
                j.runtime = 600.0

    return pending


# ── Observation builder (mirrors gym_env.py) ──────────────────────────────

def _job_feat(j: LiveJob, now: float, mps_per_gpu: int) -> np.ndarray:
    gpu_oh = [1.0 if j.gpu_type == t else 0.0 for t in GPU_TYPES]
    wait   = max(0.0, now - j.submit_ts)
    return np.array([
        j.mps_req / mps_per_gpu,
        float(j.gpu_count),
        *gpu_oh,
        math.log1p(j.runtime),
        math.log1p(wait),
        math.log1p(wait),
        0.0, 0.0,
    ], dtype=np.float32)


def _gpu_feat_live(g: LiveGpu, mps_per_gpu: int) -> np.ndarray:
    free_ratio = g.free_mps / mps_per_gpu if mps_per_gpu > 0 else 0.0
    return np.array([
        free_ratio, free_ratio, float(g.running_jobs),
        1.0, 0.0, 0.0,   # rtx4070 one-hot
    ], dtype=np.float32)


def _topo_feat(pending: List[LiveJob]) -> np.ndarray:
    ddp_ratio = sum(1 for j in pending if j.gpu_count > 1) / max(1, len(pending))
    return np.array([1.0, 1.0, ddp_ratio, 0.0], dtype=np.float32)


def _global_feat(pending: List[LiveJob], cluster: LiveCluster, now: float) -> np.ndarray:
    queue_len = len(pending)
    if len(pending) >= 2:
        rts   = sorted(j.runtime for j in pending)
        n     = len(rts)
        p50   = rts[int(n * 0.50)]
        p90   = rts[min(int(n * 0.90), n - 1)]
        spread = (p90 / p50) if p50 > 0 else 1.0
    else:
        spread = 1.0
    tod = (now % 86400) / 86400.0
    return np.array([
        math.log1p(queue_len), spread, 0.0,
        math.sin(2 * math.pi * tod),
        math.cos(2 * math.pi * tod),
        0.0,
    ], dtype=np.float32)


def build_obs_and_mask(
    pending: List[LiveJob],
    cluster: LiveCluster,
    node_names: List[str],
    n_gpus: int,
    now: float,
) -> tuple[np.ndarray, np.ndarray, List[Optional[str]]]:
    n_nodes      = len(node_names)
    mps_per_gpu  = cluster.mps_per_gpu
    n_placements = n_nodes * n_gpus
    n_actions    = TOP_K * n_placements + 1
    no_op        = n_actions - 1

    top = sorted(pending, key=lambda j: j.submit_ts)[:TOP_K]

    job_feats: List[np.ndarray] = []
    top_ids:   List[Optional[str]] = []
    for i in range(TOP_K):
        if i < len(top):
            job_feats.append(_job_feat(top[i], now, mps_per_gpu))
            top_ids.append(top[i].job_id)
        else:
            job_feats.append(np.zeros(JOB_FEAT_DIM, dtype=np.float32))
            top_ids.append(None)

    gpu_feats: List[np.ndarray] = []
    for node in node_names:
        gpus = cluster.nodes.get(node, [])
        for gi in range(n_gpus):
            if gi < len(gpus):
                gpu_feats.append(_gpu_feat_live(gpus[gi], mps_per_gpu))
            else:
                gpu_feats.append(np.zeros(GPU_FEAT_DIM, dtype=np.float32))

    topo = _topo_feat(top)
    glob = _global_feat(top, cluster, now)

    obs = np.concatenate([*job_feats, *gpu_feats, topo, glob]).astype(np.float32)

    mask = np.zeros(n_actions, dtype=bool)
    for i, j in enumerate(top):
        for nj, node in enumerate(node_names):
            gpus = cluster.nodes.get(node, [])
            for gk in range(n_gpus):
                if gk < len(gpus) and gpus[gk].free_mps >= j.mps_req:
                    a = i * n_placements + nj * n_gpus + gk
                    mask[a] = True
    mask[no_op] = True

    return obs, mask, top_ids


# ── Execution ─────────────────────────────────────────────────────────────

def execute_placement(
    job: LiveJob,
    node_name: str,
    gpu_index: int,
    *,
    shadow: bool = True,
) -> bool:
    """Issue srun with explicit placement. Returns True if submitted."""
    mps_n = job.mps_req
    cmd   = [
        "srun", f"--jobid={job.job_id}",
        f"--nodelist={node_name}",
        f"--gres=mps:{mps_n}",
        "--oversubscribe",
        "--wrap=true",   # no-op wrapper for shadow testing
    ]
    if shadow:
        print(f"[daemon] SHADOW srun {' '.join(cmd)}")
        return True
    print(f"[daemon] exec  srun {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    return r.returncode == 0


# ── Daemon loop ───────────────────────────────────────────────────────────

def run_daemon(
    *,
    agent: DSACAgent,
    node_names: List[str],
    n_gpus: int = 1,
    mps_per_gpu: int = MPS_PER_GPU,
    poll_interval: float = 30.0,
    shadow: bool = True,
    live_warmup_min: int = 0,
    value_abstain: float = -1.0,
    entropy_abstain: float = 2.5,
    log_dir: Path,
    runtime_predictor_url: Optional[str] = None,
    buf_capacity: int = 10_000,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    obs_dim, n_actions = env_dims(len(node_names), n_gpus)
    live_buf = ReplayBuffer(capacity=buf_capacity, obs_dim=obs_dim, n_actions=n_actions)
    log_path = log_dir / f"transitions_{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    decisions_made = 0

    print(f"[daemon] starting — nodes={node_names}  "
          f"shadow={shadow}  poll={poll_interval}s")
    print(f"[daemon] log → {log_path}")

    # Track submitted jobs for reward computation: job_id → (obs, act, mask, submit_ts)
    in_flight: Dict[str, tuple] = {}

    with open(log_path, "w") as log_fh:
        while True:
            now     = time.time()
            pending = query_pending_jobs(runtime_predictor_url)
            cluster = query_cluster(node_names, n_gpus, mps_per_gpu)

            if not pending:
                time.sleep(poll_interval)
                continue

            obs, mask, top_ids = build_obs_and_mask(
                pending, cluster, node_names, n_gpus, now
            )

            # Safety: wait for warmup if live data is sparse
            n_legal = int(mask.sum()) - 1  # exclude no-op
            if n_legal == 0:
                time.sleep(poll_interval)
                continue

            should_act = (len(live_buf) >= live_warmup_min)
            if should_act:
                action, value, entropy = _agent_select(agent, obs, mask)
                abstain = (value < value_abstain or entropy > entropy_abstain)
            else:
                action  = n_actions - 1  # no-op until warmup
                value   = 0.0
                entropy = 0.0
                abstain = True

            n_placements = len(node_names) * n_gpus
            no_op        = n_actions - 1

            row = {
                "ts": now, "action": action, "value": value,
                "entropy": entropy, "abstain": abstain,
                "n_pending": len(pending), "live_buf": len(live_buf),
            }

            if not abstain and action != no_op:
                job_i  = action // n_placements
                rem    = action %  n_placements
                node_j = rem // n_gpus
                gpu_k  = rem %  n_gpus
                if job_i < len(top_ids) and top_ids[job_i]:
                    sel_job_id = top_ids[job_i]
                    node_name  = node_names[node_j]
                    sel_job    = next(j for j in pending if j.job_id == sel_job_id)
                    ok = execute_placement(sel_job, node_name, gpu_k, shadow=shadow)
                    if ok:
                        in_flight[sel_job_id] = (obs.copy(), action, mask.copy(), now)
                        row["selected_job"] = sel_job_id
                        row["node"] = node_name
                        row["gpu_k"] = gpu_k
                        decisions_made += 1

            log_fh.write(json.dumps(row) + "\n")
            log_fh.flush()

            # Check for completed jobs and log transitions
            all_jobs = _run(["squeue", "--json"])
            current_ids = {j.job_id for j in _parse_squeue(all_jobs)}
            for jid, (prev_obs, prev_act, prev_mask, start_ts) in list(in_flight.items()):
                if jid not in current_ids:
                    # Job finished — compute reward as –JCT/scale
                    jct   = now - start_ts
                    rew   = -jct / 1000.0
                    # next obs is current state (after job completed)
                    next_obs, next_mask, _ = build_obs_and_mask(
                        pending, cluster, node_names, n_gpus, now
                    )
                    t = Transition(
                        obs=prev_obs, act=prev_act, rew=float(rew),
                        next_obs=next_obs, done=False,
                        mask=prev_mask, next_mask=next_mask,
                    )
                    live_buf.add(t)
                    # Log to JSONL for RLPD
                    log_fh.write(json.dumps({
                        "obs": prev_obs.tolist(), "act": prev_act,
                        "rew": float(rew), "next_obs": next_obs.tolist(),
                        "done": False,
                        "mask": prev_mask.tolist(),
                        "next_mask": next_mask.tolist(),
                        "jct_s": jct,
                    }) + "\n")
                    del in_flight[jid]

            print(f"[daemon] {time.strftime('%H:%M:%S')}  "
                  f"pending={len(pending)}  decisions={decisions_made}  "
                  f"live_buf={len(live_buf)}  abstain={abstain}")
            time.sleep(poll_interval)


def _agent_select(
    agent: DSACAgent, obs: np.ndarray, mask: np.ndarray
) -> tuple[int, float, float]:
    import torch
    with torch.no_grad():
        obs_t  = torch.as_tensor(obs,  dtype=torch.float32).unsqueeze(0)
        mask_t = torch.as_tensor(mask, dtype=torch.bool).unsqueeze(0)
        q      = torch.min(agent.q1(obs_t), agent.q2(obs_t))
        probs, log_probs = agent._masked_policy(q, mask_t)
        entropy = float(-(probs * log_probs).sum(dim=-1).item())
        value   = float((probs * q).sum(dim=-1).item())
        pnp     = probs.squeeze(0).cpu().numpy()
    pnp = pnp * mask.astype(np.float32)
    s   = pnp.sum()
    action = int(pnp.argmax()) if s > 1e-9 else int(np.flatnonzero(mask)[0])
    return action, value, entropy


# ── Entry ─────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--policy-dir",    required=True)
    p.add_argument("--node-name",     nargs="+", required=True,
                   help="Slurm node name(s) to manage")
    p.add_argument("--gpus-per-node", type=int, default=1)
    p.add_argument("--mps-per-gpu",   type=int, default=MPS_PER_GPU)
    p.add_argument("--poll-interval", type=float, default=30.0)
    p.add_argument("--log-dir",       default="shadow_logs")
    p.add_argument("--live-warmup",   type=int, default=0,
                   help="min live transitions before agent takes control")
    p.add_argument("--predictor-url", default=None,
                   help="runtime predictor API URL for predicted runtimes")
    p.add_argument("--buf-capacity",  type=int, default=10_000)
    args = p.parse_args(argv)

    shadow = os.environ.get("SHADOW_MODE", "true").lower() in ("1", "true", "yes")
    if shadow:
        print("[daemon] SHADOW_MODE=true — decisions logged but srun not executed")

    ckpt = Path(args.policy_dir) / "dsac.pt"
    print(f"[daemon] loading agent from {ckpt}")
    agent = DSACAgent.load(str(ckpt))

    run_daemon(
        agent=agent,
        node_names=args.node_name,
        n_gpus=args.gpus_per_node,
        mps_per_gpu=args.mps_per_gpu,
        poll_interval=args.poll_interval,
        shadow=shadow,
        live_warmup_min=args.live_warmup,
        log_dir=Path(args.log_dir),
        runtime_predictor_url=args.predictor_url,
        buf_capacity=args.buf_capacity,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
