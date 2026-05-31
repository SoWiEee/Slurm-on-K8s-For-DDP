"""DSAC scheduling server — FastAPI inference endpoint for the live cluster.

Loads a DSACAgent checkpoint and exposes:

  GET  /healthz        — readiness probe
  POST /snapshot       — push current cluster state (sidecar)
  POST /decide         — Lua hook: given a submitting job, return
                         priority_boost + placement (node_j, gpu_k)
  POST /act            — raw obs → action (for testing / daemon use)

Backward-compatible with the Lua rl_hook.lua client: /decide still returns
``priority_boost`` so job_submit.lua can influence Slurm ordering without
changes. The new ``node_j`` / ``gpu_k`` fields let live_daemon.py issue
srun with explicit placement.

Run::
    .venv-m11/bin/python -m services.rl_scheduler.serve \\
        --policy-dir runs/dsac_sim --port 8002
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    generate_latest,
)
from pydantic import BaseModel, Field
from starlette.responses import Response

from services.rl_scheduler import serve_otel as _otel

from sim.gym_env import (
    GPU_FEAT_DIM, GPU_TYPES,
    GLOBAL_FEAT_DIM, JOB_FEAT_DIM, MPS_PER_GPU,
    TOPO_FEAT_DIM, TOP_K,
    env_dims,
)
from services.rl_scheduler.dsac import DSACAgent


# ── Request / response schemas ─────────────────────────────────────────────

class JobView(BaseModel):
    job_id:    str
    mps_req:   int
    gpu_count: int
    gpu_type:  str   = "rtx4070"
    runtime:   float             # predicted seconds (oracle in sim)
    submit_ts: float
    can_fit:   bool  = True      # caller-side feasibility hint


class GpuView(BaseModel):
    free_mps:     int
    running_jobs: int   = 0
    gpu_type:     str   = "rtx4070"


class NodeView(BaseModel):
    gpus: List[GpuView] = Field(default_factory=list)


class ActRequest(BaseModel):
    now:          float
    pending_jobs: List[JobView] = Field(default_factory=list)
    nodes:        List[NodeView] = Field(default_factory=list)
    n_nodes:      int = 1
    gpus_per_node: int = 1
    mps_per_gpu:  int = MPS_PER_GPU


class ActResponse(BaseModel):
    action:           int
    job_i:            Optional[int]
    node_j:           Optional[int]
    gpu_k:            Optional[int]
    selected_job_id:  Optional[str]
    value:            float
    entropy:          float


class Snapshot(BaseModel):
    ts:           float = Field(default_factory=time.time)
    now:          float
    pending_jobs: List[JobView] = Field(default_factory=list)
    nodes:        List[NodeView] = Field(default_factory=list)
    n_nodes:      int = 1
    gpus_per_node: int = 1
    mps_per_gpu:  int = MPS_PER_GPU


class DecideRequest(BaseModel):
    """Lua sends only the current submitting job; serve fuses with snapshot."""
    job_id:    str
    mps_req:   int
    gpu_count: int
    gpu_type:  str   = "rtx4070"
    runtime:   float
    submit_ts: float


class DecideResponse(BaseModel):
    priority_boost:     int
    rl_selected:        bool
    abstain:            bool
    abstain_reason:     Optional[str]
    rl_selected_job_id: Optional[str]
    node_j:             Optional[int]    # placement: node index (0-based)
    gpu_k:              Optional[int]    # placement: gpu index (0-based)
    otel_traceparent:   Optional[str] = None  # W3C traceparent for Phase 7-A OTel
    value:              float
    entropy:            float
    shadow:             bool


# ── Feature construction (mirrors sim/gym_env.py exactly) ─────────────────

def _job_feat(j: JobView, now: float, mps_per_gpu: int) -> np.ndarray:
    gpu_oh = [1.0 if j.gpu_type == t else 0.0 for t in GPU_TYPES]
    wait   = max(0.0, now - j.submit_ts)
    return np.array([
        j.mps_req / mps_per_gpu,
        float(j.gpu_count),
        *gpu_oh,                      # 4 dims
        math.log1p(j.runtime),
        math.log1p(wait),
        math.log1p(wait),             # age (matches gym_env.py placeholder)
        0.0,                          # deadline_remaining placeholder
        0.0,                          # retry_count placeholder
    ], dtype=np.float32)              # 11 dims total


def _gpu_feat(g: GpuView, mps_per_gpu: int) -> np.ndarray:
    free_ratio = g.free_mps / mps_per_gpu if mps_per_gpu > 0 else 0.0
    gpu_oh     = [1.0, 0.0, 0.0]  # rtx4070 / other_a / other_b (homogeneous)
    return np.array([
        free_ratio,
        free_ratio,                   # vram proxy (same scale as MPS in sim)
        float(g.running_jobs),
        *gpu_oh,
    ], dtype=np.float32)              # 6 dims


def _topo_feat(pending: List[JobView], n_nodes: int) -> np.ndarray:
    ddp_ratio  = sum(1 for j in pending if j.gpu_count > 1) / max(1, len(pending))
    cross_node = 0.0   # live daemon fills this when known
    return np.array([1.0, 1.0, ddp_ratio, cross_node], dtype=np.float32)


def _global_feat(pending: List[JobView], nodes: List[NodeView],
                  now: float, mps_per_gpu: int) -> np.ndarray:
    queue_len = len(pending)
    if len(pending) >= 2:
        rts   = sorted(j.runtime for j in pending)
        n     = len(rts)
        p50   = rts[int(n * 0.50)]
        p90   = rts[min(int(n * 0.90), n - 1)]
        spread = (p90 / p50) if p50 > 0 else 1.0
    else:
        spread = 1.0
    free_per_node = [
        sum(g.free_mps for g in nd.gpus) for nd in nodes
    ] if nodes else [0]
    if len(free_per_node) > 1:
        mean  = max(1.0, sum(free_per_node) / len(free_per_node))
        var   = sum((x - mean) ** 2 for x in free_per_node) / len(free_per_node)
        frag  = math.sqrt(var) / mean
    else:
        frag  = 0.0
    tod = (now % 86400) / 86400.0
    return np.array([
        math.log1p(queue_len), spread, frag,
        math.sin(2 * math.pi * tod),
        math.cos(2 * math.pi * tod),
        0.0,
    ], dtype=np.float32)             # 6 dims


def build_obs_and_mask(
    req: ActRequest,
) -> tuple[np.ndarray, np.ndarray, List[Optional[str]]]:
    """Return (obs, action_mask, top_k_job_ids).

    Observation layout matches gym_env.py exactly:
      TOP_K × JOB_FEAT_DIM + n_nodes×n_gpus×GPU_FEAT_DIM + TOPO + GLOBAL
    Action mask shape: (n_actions,) where n_actions = TOP_K*n_nodes*n_gpus + 1.
    """
    n_nodes      = req.n_nodes
    n_gpus       = req.gpus_per_node
    mps_per_gpu  = req.mps_per_gpu
    n_placements = n_nodes * n_gpus
    n_actions    = TOP_K * n_placements + 1
    no_op        = n_actions - 1

    pending_sorted = sorted(req.pending_jobs, key=lambda j: j.submit_ts)[:TOP_K]

    # Job feats
    job_feats: List[np.ndarray] = []
    top_ids:   List[Optional[str]] = []
    for i in range(TOP_K):
        if i < len(pending_sorted):
            j = pending_sorted[i]
            job_feats.append(_job_feat(j, req.now, mps_per_gpu))
            top_ids.append(j.job_id)
        else:
            job_feats.append(np.zeros(JOB_FEAT_DIM, dtype=np.float32))
            top_ids.append(None)

    # GPU slot feats
    gpu_feats: List[np.ndarray] = []
    for ni in range(n_nodes):
        for gi in range(n_gpus):
            if ni < len(req.nodes) and gi < len(req.nodes[ni].gpus):
                gpu_feats.append(_gpu_feat(req.nodes[ni].gpus[gi], mps_per_gpu))
            else:
                gpu_feats.append(np.zeros(GPU_FEAT_DIM, dtype=np.float32))

    topo = _topo_feat(pending_sorted, n_nodes)
    glob = _global_feat(list(req.pending_jobs), req.nodes, req.now, mps_per_gpu)

    obs = np.concatenate([*job_feats, *gpu_feats, topo, glob]).astype(np.float32)

    # Action mask: True iff (job_i, node_j, gpu_k) is feasible
    mask = np.zeros(n_actions, dtype=bool)
    for i, j in enumerate(pending_sorted):
        if not j.can_fit:
            continue
        for nj in range(n_nodes):
            if nj >= len(req.nodes):
                continue
            for gk in range(n_gpus):
                if gk >= len(req.nodes[nj].gpus):
                    continue
                gpu = req.nodes[nj].gpus[gk]
                if gpu.free_mps >= j.mps_req:
                    a = i * n_placements + nj * n_gpus + gk
                    mask[a] = True
    mask[no_op] = True

    return obs, mask, top_ids


# ── Model holder ──────────────────────────────────────────────────────────

class _AgentHolder:
    def __init__(self, policy_dir: Path, n_nodes: int = 1, gpus_per_node: int = 1):
        ckpt = policy_dir / "dsac.pt"
        if not ckpt.exists():
            raise FileNotFoundError(f"no dsac.pt under {policy_dir}")
        self.agent = DSACAgent.load(str(ckpt))
        self.policy_dir = policy_dir
        print(f"[serve] loaded DSACAgent obs_dim={self.agent.obs_dim} "
              f"n_actions={self.agent.n_actions} from {ckpt}")

    def select(
        self, obs: np.ndarray, mask: np.ndarray
    ) -> tuple[int, float, float]:
        """Return (action, value, entropy)."""
        import torch
        with torch.no_grad():
            obs_t  = torch.as_tensor(obs,  dtype=torch.float32).unsqueeze(0)
            mask_t = torch.as_tensor(mask, dtype=torch.bool).unsqueeze(0)
            q      = torch.min(self.agent.q1(obs_t), self.agent.q2(obs_t))
            probs, log_probs = self.agent._masked_policy(q, mask_t)
            entropy  = float(-(probs * log_probs).sum(dim=-1).item())
            value    = float((probs * q).sum(dim=-1).item())
            probs_np = probs.squeeze(0).cpu().numpy()

        probs_np = probs_np * mask.astype(np.float32)
        total    = probs_np.sum()
        if total < 1e-9:
            action = int(np.flatnonzero(mask)[0])
        else:
            action = int(probs_np.argmax())   # greedy (serve = inference only)
        return action, value, entropy


# ── App state ─────────────────────────────────────────────────────────────

SNAPSHOT_TTL_S  = float(os.environ.get("SNAPSHOT_TTL_S", "30"))
SHADOW_MODE     = os.environ.get("SHADOW_MODE", "true").lower() in ("1", "true", "yes")
VALUE_ABSTAIN   = float(os.environ.get("VALUE_ABSTAIN", "-1.0"))
ENTROPY_ABSTAIN = float(os.environ.get("ENTROPY_ABSTAIN", "2.5"))
PRIORITY_BOOST  = int(os.environ.get("PRIORITY_BOOST", "1000"))

_holder:   Optional[_AgentHolder] = None
_snapshot: Optional[Snapshot]     = None

app = FastAPI(title="kubeflux-dsac-scheduler", version="1.0")

METRICS_REGISTRY = CollectorRegistry()
RL_DECISIONS = Counter(
    "rl_scheduler_decisions_total",
    "DSAC scheduler decisions by result",
    ["result"],
    registry=METRICS_REGISTRY,
)
RL_PRIORITY_BOOSTS = Counter(
    "rl_scheduler_priority_boost_total",
    "DSAC decisions that returned a positive priority boost",
    registry=METRICS_REGISTRY,
)
RL_POLICY_VALUE = Gauge(
    "rl_scheduler_policy_value",
    "Value estimate for the last DSAC decision",
    registry=METRICS_REGISTRY,
)
RL_POLICY_ENTROPY = Gauge(
    "rl_scheduler_policy_entropy",
    "Policy entropy for the last DSAC decision",
    registry=METRICS_REGISTRY,
)
RL_SNAPSHOT_AGE = Gauge(
    "rl_scheduler_snapshot_age_seconds",
    "Age of the cached cluster snapshot",
    registry=METRICS_REGISTRY,
)
RL_SNAPSHOT_PENDING = Gauge(
    "rl_scheduler_snapshot_pending_jobs",
    "Pending jobs in the cached snapshot",
    registry=METRICS_REGISTRY,
)
RL_SNAPSHOT_FREE_MPS = Gauge(
    "rl_scheduler_snapshot_free_mps",
    "Total free MPS slots in the cached snapshot",
    registry=METRICS_REGISTRY,
)
RL_LAST_PRIORITY_BOOST = Gauge(
    "rl_scheduler_last_priority_boost",
    "Priority boost returned by the last DSAC decision",
    registry=METRICS_REGISTRY,
)
RL_LAST_ACTION = Gauge(
    "rl_scheduler_last_action",
    "Flat action index selected by the last DSAC decision",
    registry=METRICS_REGISTRY,
)
RL_LAST_JOB_INDEX = Gauge(
    "rl_scheduler_last_job_index",
    "Job slot selected by the last DSAC decision; -1 for no-op or abstain",
    registry=METRICS_REGISTRY,
)
RL_LAST_NODE_INDEX = Gauge(
    "rl_scheduler_last_node_index",
    "Node index selected by the last DSAC decision; -1 for no-op or abstain",
    registry=METRICS_REGISTRY,
)
RL_LAST_GPU_INDEX = Gauge(
    "rl_scheduler_last_gpu_index",
    "GPU index selected by the last DSAC decision; -1 for no-op or abstain",
    registry=METRICS_REGISTRY,
)
RL_SHADOW_MODE = Gauge(
    "rl_scheduler_shadow_mode",
    "1 when DSAC scheduler is in shadow mode, 0 when live",
    registry=METRICS_REGISTRY,
)
RL_READY = Gauge(
    "rl_scheduler_ready",
    "1 when the DSAC model is loaded",
    registry=METRICS_REGISTRY,
)

RL_SHADOW_MODE.set(1.0 if SHADOW_MODE else 0.0)
RL_READY.set(0.0)
for _result in ("selected", "no_boost", "abstain"):
    RL_DECISIONS.labels(result=_result)


def _set_last_decision(
    *,
    result: str,
    value: float,
    entropy: float,
    boost: int,
    action: int = -1,
    job_i: int = -1,
    node_j: int = -1,
    gpu_k: int = -1,
) -> None:
    RL_DECISIONS.labels(result=result).inc()
    if boost > 0:
        RL_PRIORITY_BOOSTS.inc()
    RL_POLICY_VALUE.set(value)
    RL_POLICY_ENTROPY.set(entropy)
    RL_LAST_PRIORITY_BOOST.set(boost)
    RL_LAST_ACTION.set(action)
    RL_LAST_JOB_INDEX.set(job_i)
    RL_LAST_NODE_INDEX.set(node_j)
    RL_LAST_GPU_INDEX.set(gpu_k)


# ── Endpoints ──────────────────────────────────────────────────────────────

@app.get("/healthz")
def healthz():
    snap_age = (time.time() - _snapshot.ts) if _snapshot else None
    RL_READY.set(1.0 if _holder is not None else 0.0)
    if snap_age is not None:
        RL_SNAPSHOT_AGE.set(snap_age)
    return {
        "ready": _holder is not None,
        "obs_dim": _holder.agent.obs_dim if _holder else None,
        "n_actions": _holder.agent.n_actions if _holder else None,
        "snapshot_age_s": snap_age,
        "shadow_mode": SHADOW_MODE,
    }


@app.get("/metrics")
def metrics():
    snap_age = (time.time() - _snapshot.ts) if _snapshot else None
    if snap_age is not None:
        RL_SNAPSHOT_AGE.set(snap_age)
    return Response(generate_latest(METRICS_REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.post("/snapshot")
def push_snapshot(snap: Snapshot):
    global _snapshot
    _snapshot = snap
    free_mps = sum(g.free_mps for node in snap.nodes for g in node.gpus)
    RL_SNAPSHOT_AGE.set(0.0)
    RL_SNAPSHOT_PENDING.set(len(snap.pending_jobs))
    RL_SNAPSHOT_FREE_MPS.set(free_mps)
    return {"ok": True, "ts": snap.ts,
            "pending": len(snap.pending_jobs), "nodes": len(snap.nodes)}


@app.post("/act", response_model=ActResponse)
def act(req: ActRequest):
    if _holder is None:
        raise HTTPException(status_code=503, detail="model not loaded")

    obs, mask, top_ids = build_obs_and_mask(req)
    action, value, entropy = _holder.select(obs, mask)

    n_placements = req.n_nodes * req.gpus_per_node
    no_op        = TOP_K * n_placements
    if action == no_op:
        return ActResponse(action=action, job_i=None, node_j=None, gpu_k=None,
                           selected_job_id=None, value=value, entropy=entropy)

    job_i  = action // n_placements
    rem    = action %  n_placements
    node_j = rem // req.gpus_per_node
    gpu_k  = rem %  req.gpus_per_node
    return ActResponse(
        action=action, job_i=job_i, node_j=node_j, gpu_k=gpu_k,
        selected_job_id=top_ids[job_i] if job_i < len(top_ids) else None,
        value=value, entropy=entropy,
    )


@app.post("/decide", response_model=DecideResponse)
def decide(req: DecideRequest):
    snap = _snapshot
    age  = (time.time() - snap.ts) if snap else None
    if snap is None or age is None or age > SNAPSHOT_TTL_S:
        _set_last_decision(result="abstain", value=0.0, entropy=0.0, boost=0)
        if age is not None:
            RL_SNAPSHOT_AGE.set(age)
        return DecideResponse(
            priority_boost=0, rl_selected=False, abstain=True,
            abstain_reason=f"snapshot_stale (age={age}s)",
            rl_selected_job_id=None, node_j=None, gpu_k=None,
            value=0.0, entropy=0.0, shadow=SHADOW_MODE,
        )

    # Fuse submitting job into snapshot's pending list
    fused = list(snap.pending_jobs)
    if not any(j.job_id == req.job_id for j in fused):
        fused.append(JobView(
            job_id=req.job_id, mps_req=req.mps_req,
            gpu_count=req.gpu_count, gpu_type=req.gpu_type,
            runtime=req.runtime, submit_ts=req.submit_ts, can_fit=True,
        ))

    act_req = ActRequest(
        now=max(snap.now, req.submit_ts),
        pending_jobs=fused, nodes=snap.nodes,
        n_nodes=snap.n_nodes, gpus_per_node=snap.gpus_per_node,
        mps_per_gpu=snap.mps_per_gpu,
    )
    obs, mask, top_ids = build_obs_and_mask(act_req)
    action, value, entropy = _holder.select(obs, mask)

    n_placements = snap.n_nodes * snap.gpus_per_node
    no_op        = TOP_K * n_placements

    if action == no_op:
        _set_last_decision(
            result="no_boost", value=value, entropy=entropy, boost=0, action=action,
        )
        return DecideResponse(
            priority_boost=0, rl_selected=False, abstain=False,
            abstain_reason=None, rl_selected_job_id=None,
            node_j=None, gpu_k=None, value=value, entropy=entropy,
            shadow=SHADOW_MODE,
        )

    job_i  = action // n_placements
    rem    = action %  n_placements
    node_j = rem // snap.gpus_per_node
    gpu_k  = rem %  snap.gpus_per_node
    sel_id = top_ids[job_i] if job_i < len(top_ids) else None

    # Safety net
    abstain = False
    reason  = None
    if value < VALUE_ABSTAIN:
        abstain = True
        reason  = f"low_value ({value:.3f} < {VALUE_ABSTAIN})"
    elif entropy > ENTROPY_ABSTAIN:
        abstain = True
        reason  = f"high_entropy ({entropy:.3f} > {ENTROPY_ABSTAIN})"

    rl_picked_me = (sel_id == req.job_id)
    if SHADOW_MODE or abstain:
        boost = 0
    else:
        boost = PRIORITY_BOOST if rl_picked_me else 0

    result = "abstain" if abstain else ("selected" if boost > 0 else "no_boost")
    _set_last_decision(
        result=result, value=value, entropy=entropy, boost=boost, action=action,
        job_i=(-1 if abstain else job_i),
        node_j=(-1 if abstain else node_j),
        gpu_k=(-1 if abstain else gpu_k),
    )

    # OTel Phase 7-A: emit job_submit span; pass traceparent back so the Lua
    # hook can write it to job_desc.admin_comment as "otel=<traceparent>".
    traceparent = ""
    if _otel.enabled():
        with _otel.job_submit_span(
            job_id=req.job_id,
            partition=getattr(_snapshot, "partition", "unknown") if _snapshot else "unknown",
            gres=f"gpu:{req.gpu_count}" if req.gpu_count else "",
            requested_cpus=0,
        ) as tp:
            traceparent = tp

    return DecideResponse(
        priority_boost=boost, rl_selected=rl_picked_me,
        abstain=abstain, abstain_reason=reason,
        rl_selected_job_id=sel_id,
        node_j=node_j if not abstain else None,
        gpu_k=gpu_k  if not abstain else None,
        value=value, entropy=entropy, shadow=SHADOW_MODE,
        otel_traceparent=traceparent or None,
    )


# ── Entry ──────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--policy-dir",    required=True)
    p.add_argument("--n-nodes",       type=int, default=1)
    p.add_argument("--gpus-per-node", type=int, default=1)
    p.add_argument("--host",          default="0.0.0.0")
    p.add_argument("--port",          type=int, default=8002)
    args = p.parse_args(argv)

    global _holder
    _holder = _AgentHolder(
        Path(args.policy_dir),
        n_nodes=args.n_nodes, gpus_per_node=args.gpus_per_node,
    )
    RL_READY.set(1.0)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
