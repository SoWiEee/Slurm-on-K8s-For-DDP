"""M11 Phase C-4: RLPD-style Sim2Real fine-tune scaffold.

RLPD (Ball et al., ICML 2023) — "Efficient Online RL with Offline Data".
Key idea: keep an offline replay buffer (from sim) and an online buffer
(from live cluster), and each training batch is half-sampled from each.
LayerNorm + high UTD ratio = update-to-data 20:1 close the sim-to-real gap
in ~10^3 live transitions.

This file is the scaffold — actual fine-tuning runs once Phase D has
emitted enough shadow-mode transitions for the online buffer to be non-trivial.
The PPO-on-MaskablePPO path doesn't natively support off-policy replay, so
RLPD here uses SAC-style off-policy: load the sim-trained policy + value
heads as warm-start, then keep training off the mixed replay buffer.

Pieces:
  ReplayBuffer       — minimal numpy-backed FIFO buffer (obs/act/rew/next_obs/done)
  SimRolloutCollector — drive sim.gym_env to fill the offline buffer
  LiveTransitionLogger — append to online buffer from a parsed shadow log
  rlpd_train         — half-half sampler + warm-start fine-tune loop

Notes / honest caveats:
  - The sim policy is MaskablePPO with discrete action space. The cleanest
    RLPD port would be MaskableDQN or MaskableSAC — both are missing from
    sb3-contrib at time of writing. As a pragmatic stand-in this scaffold
    runs PPO on the mixed buffer with importance correction off (so it's
    biased but cheap); document the bias when reporting numbers.
  - "Sim2Real gap" in our setting: sim ignores k8s pod start latency,
    image-pull jitter, NFS write spikes, and real Slurm prio inflation.
    The online buffer is what teaches the policy these.

Run (once a shadow log is available — Phase D):
    .venv-m11/bin/python -m services.rl_scheduler.rlpd_finetune \\
        --base-policy runs/m11_mppo_20260512-185937 \\
        --offline-steps 50000 --online-log shadow_logs/*.jsonl \\
        --out-dir runs/m11_rlpd_$(date +%Y%m%d-%H%M%S)
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

try:
    from sim.gym_env import KubefluxSchedGymEnv
except ImportError:
    KubefluxSchedGymEnv = None  # type: ignore


@dataclass
class Transition:
    obs: np.ndarray
    act: int
    rew: float
    next_obs: np.ndarray
    done: bool
    mask: Optional[np.ndarray] = None       # action_mask at obs
    next_mask: Optional[np.ndarray] = None  # action_mask at next_obs (needed by DSAC critic)


@dataclass
class ReplayBuffer:
    """FIFO numpy-backed replay. Stored as parallel arrays for cheap batch
    sampling. Pre-allocates to capacity to avoid repeated np.append."""
    capacity: int
    obs_dim: int
    n_actions: int
    obs: np.ndarray = field(init=False)
    acts: np.ndarray = field(init=False)
    rews: np.ndarray = field(init=False)
    next_obs: np.ndarray = field(init=False)
    dones: np.ndarray = field(init=False)
    masks: np.ndarray = field(init=False)
    next_masks: np.ndarray = field(init=False)
    _size: int = 0
    _idx: int = 0

    def __post_init__(self):
        self.obs = np.zeros((self.capacity, self.obs_dim), dtype=np.float32)
        self.acts = np.zeros((self.capacity,), dtype=np.int64)
        self.rews = np.zeros((self.capacity,), dtype=np.float32)
        self.next_obs = np.zeros((self.capacity, self.obs_dim), dtype=np.float32)
        self.dones = np.zeros((self.capacity,), dtype=np.bool_)
        self.masks = np.ones((self.capacity, self.n_actions), dtype=np.bool_)
        self.next_masks = np.ones((self.capacity, self.n_actions), dtype=np.bool_)

    def __len__(self) -> int:
        return self._size

    def add(self, t: Transition) -> None:
        i = self._idx
        self.obs[i] = t.obs
        self.acts[i] = t.act
        self.rews[i] = t.rew
        self.next_obs[i] = t.next_obs
        self.dones[i] = t.done
        if t.mask is not None:
            self.masks[i] = t.mask
        if t.next_mask is not None:
            self.next_masks[i] = t.next_mask
        self._idx = (i + 1) % self.capacity
        self._size = min(self._size + 1, self.capacity)

    def sample(self, n: int, rng: np.random.Generator) -> dict:
        idx = rng.integers(0, self._size, size=n)
        return {
            "obs": self.obs[idx], "acts": self.acts[idx],
            "rews": self.rews[idx], "next_obs": self.next_obs[idx],
            "dones": self.dones[idx], "masks": self.masks[idx],
            "next_masks": self.next_masks[idx],
        }


def collect_sim_rollouts(*, n_transitions: int, trace_family: str,
                         n_jobs: int, n_nodes: int, gpus_per_node: int,
                         seed: int = 42) -> ReplayBuffer:
    """Drive sim.gym_env with a uniform-random masked policy to fill an
    offline buffer. We deliberately don't use the trained policy here —
    we want diverse states, not the policy's narrow trajectory."""
    from sim.gym_env import KubefluxSchedGymEnv
    from sim.loader import generate_by_family

    rng = np.random.default_rng(seed)

    def _factory():
        return generate_by_family(trace_family, n_jobs=n_jobs,
                                  seed=int(rng.integers(0, 2**31 - 1)))

    env = KubefluxSchedGymEnv(
        jobs_factory=_factory,
        n_nodes=n_nodes,
        gpus_per_node=gpus_per_node,
        max_steps=n_jobs * 100,
    )
    obs_dim = int(np.prod(env.observation_space.shape))
    n_actions = int(env.action_space.n)
    buf = ReplayBuffer(capacity=n_transitions, obs_dim=obs_dim,
                       n_actions=n_actions)

    obs, _ = env.reset()
    while len(buf) < n_transitions:
        mask = env.action_masks().astype(bool)
        legal = np.flatnonzero(mask)
        act = int(rng.choice(legal)) if len(legal) else 0
        next_obs, rew, terminated, truncated, _info = env.step(act)
        next_mask = env.action_masks().astype(bool)
        buf.add(Transition(
            obs=obs.astype(np.float32).reshape(-1),
            act=act, rew=float(rew),
            next_obs=next_obs.astype(np.float32).reshape(-1),
            done=bool(terminated or truncated),
            mask=mask,
            next_mask=next_mask,
        ))
        obs = next_obs
        if terminated or truncated:
            obs, _ = env.reset()
    env.close()
    return buf


def load_live_shadow_log(paths: list[str], *, obs_dim: int,
                          n_actions: int, capacity: int) -> ReplayBuffer:
    """Parse Phase D shadow-mode log lines (one JSON per /decide call) and
    materialise them as transitions.

    Expected line schema (emitted by Phase D log shipper — TBD):
        {"obs": [...], "act": int, "rew": float,
         "next_obs": [...], "done": bool, "mask": [bool ...]}

    Real-cluster reward is computed offline by joining each /decide row
    with the eventual JCT of the selected job (see Phase D pipeline)."""
    buf = ReplayBuffer(capacity=capacity, obs_dim=obs_dim, n_actions=n_actions)
    files = []
    for p in paths:
        files.extend(glob.glob(p))
    if not files:
        print(f"[rlpd] no shadow log files matched {paths}; "
              f"online buffer will be empty", file=sys.stderr)
        return buf
    for fp in files:
        with open(fp) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "obs" not in row or "next_obs" not in row:
                    continue
                buf.add(Transition(
                    obs=np.asarray(row["obs"], dtype=np.float32),
                    act=int(row.get("act", 0)),
                    rew=float(row.get("rew", 0.0)),
                    next_obs=np.asarray(row["next_obs"], dtype=np.float32),
                    done=bool(row.get("done", False)),
                    mask=np.asarray(row.get("mask",
                                            [True] * n_actions),
                                    dtype=bool),
                ))
    return buf


def mixed_batch(*, offline: ReplayBuffer, online: ReplayBuffer,
                batch_size: int, online_ratio: float,
                rng: np.random.Generator) -> dict:
    """RLPD core: each batch is online_ratio from live, rest from sim.
    If online is empty (e.g. cold-start before Phase D), fall back to
    100% offline."""
    if len(online) == 0:
        return offline.sample(batch_size, rng)
    n_online = max(1, int(batch_size * online_ratio))
    n_offline = batch_size - n_online
    a = online.sample(n_online, rng)
    b = offline.sample(n_offline, rng)
    out = {}
    for k in a:
        out[k] = np.concatenate([a[k], b[k]], axis=0)
    return out


def rlpd_train(*, base_policy_dir: Path, offline: ReplayBuffer,
               online: ReplayBuffer, n_updates: int,
               utd_ratio: int, batch_size: int, online_ratio: float,
               out_dir: Path,
               trace_family: str = "philly", n_jobs: int = 100,
               n_nodes: int = 2, gpus_per_node: int = 2) -> None:
    """Real DSAC-based RLPD fine-tune (replaces the biased PPO scaffold).

    Uses Discrete SAC (Christodoulou 2019) with action masking + LayerNorm.
    Each gradient step draws a mixed batch: online_ratio from live data,
    rest from sim. UTD ratio controls how many gradient steps per env step.
    """
    from .dsac import DSACAgent
    from sim.runner import run as sim_run

    out_dir.mkdir(parents=True, exist_ok=True)
    obs_dim = offline.obs_dim
    n_actions = offline.n_actions

    warm_start = out_dir / "dsac.pt"
    if warm_start.exists():
        print(f"[rlpd] warm-starting DSAC from {warm_start}")
        agent = DSACAgent.load(warm_start)
    else:
        agent = DSACAgent(obs_dim=obs_dim, n_actions=n_actions, device="cpu")

    rng = np.random.default_rng(0)
    log_path = out_dir / "rlpd_train.jsonl"

    print(f"[rlpd] DSAC training: {n_updates} updates × UTD={utd_ratio} "
          f"offline={len(offline)} online={len(online)}")

    with open(log_path, "w") as fh:
        for update in range(n_updates):
            loss_acc: dict = {}
            for _ in range(utd_ratio):
                batch = mixed_batch(offline=offline, online=online,
                                    batch_size=batch_size,
                                    online_ratio=online_ratio, rng=rng)
                losses = agent.update(batch)
                for k, v in losses.items():
                    loss_acc[k] = loss_acc.get(k, 0.0) + v / utd_ratio

            row = {"update": update, "online_size": len(online),
                   "offline_size": len(offline), **loss_acc}
            fh.write(json.dumps(row) + "\n")

            if (update + 1) % 50 == 0:
                print(f"  update {update+1:4d}/{n_updates}  "
                      f"loss_q={loss_acc.get('loss_q', 0):.4f}  "
                      f"alpha={loss_acc.get('alpha', 0):.4f}  "
                      f"H={loss_acc.get('entropy', 0):.3f}")

        agent.save(warm_start)

    # Quick eval: 3 greedy episodes vs score baseline
    print("\n[rlpd] evaluating DSAC (3 greedy episodes)...")
    from sim.loader import generate_by_family
    dsac_jcts = []
    for ep_seed in [42, 43, 44]:
        jobs = generate_by_family(trace_family, n_jobs=n_jobs, seed=ep_seed)
        env = KubefluxSchedGymEnv(
            jobs_factory=lambda _s=ep_seed: generate_by_family(trace_family, n_jobs=n_jobs, seed=_s),
            n_nodes=n_nodes, gpus_per_node=gpus_per_node,
            max_steps=n_jobs * 100, reward_mode="jct_aligned",
        )
        obs, _ = env.reset()
        done = False
        info = {}
        while not done:
            mask = env.action_masks()
            act = agent.select_action(obs, mask, greedy=True)
            obs, _, terminated, truncated, info = env.step(act)
            done = terminated or truncated
        env.close()
        jct = info.get("avg_jct", float("nan"))
        dsac_jcts.append(jct)

    score_jcts = []
    for ep_seed in [42, 43, 44]:
        jobs = generate_by_family(trace_family, n_jobs=n_jobs, seed=ep_seed)
        metrics, _ = sim_run(jobs, n_nodes=n_nodes, gpus_per_node=gpus_per_node,
                              scheduler_name="score")
        score_jcts.append(metrics.summary()["jct_mean"])

    dsac_mean = float(np.nanmean(dsac_jcts))
    score_mean = float(np.mean(score_jcts))
    pct = (score_mean - dsac_mean) / score_mean * 100
    print(f"\n  DSAC mean JCT : {dsac_mean/3600:.3f}h")
    print(f"  Score baseline: {score_mean/3600:.3f}h")
    print(f"  Δ(score-DSAC) : {pct:+.1f}%  "
          f"({'DSAC wins' if pct > 0 else 'score wins'})")
    print(f"\n[rlpd] policy saved: {warm_start}")
    print(f"[rlpd] training log: {log_path}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--base-policy", default=None,
                   help="dir with dsac.pt checkpoint to warm-start from (optional)")
    p.add_argument("--offline-steps", type=int, default=50_000)
    p.add_argument("--online-log", nargs="*", default=[])
    p.add_argument("--n-updates", type=int, default=200)
    p.add_argument("--utd-ratio", type=int, default=20)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--online-ratio", type=float, default=0.5)
    p.add_argument("--trace-family", default="philly")
    p.add_argument("--n-jobs", type=int, default=300)
    p.add_argument("--n-nodes", type=int, default=2)
    p.add_argument("--gpus-per-node", type=int, default=2)
    p.add_argument("--out-dir",
                   default=f"runs/m11_rlpd_{time.strftime('%Y%m%d-%H%M%S')}")
    args = p.parse_args(argv)

    base = Path(args.base_policy) if args.base_policy else Path(args.out_dir)

    print(f"[rlpd] collecting offline buffer ({args.offline_steps} steps)...")
    offline = collect_sim_rollouts(
        n_transitions=args.offline_steps,
        trace_family=args.trace_family,
        n_jobs=args.n_jobs,
        n_nodes=args.n_nodes,
        gpus_per_node=args.gpus_per_node,
    )
    print(f"[rlpd] offline buffer size = {len(offline)}")

    online = load_live_shadow_log(
        args.online_log,
        obs_dim=offline.obs.shape[1],
        n_actions=offline.masks.shape[1],
        capacity=max(10_000, args.offline_steps),
    )
    print(f"[rlpd] online buffer size = {len(online)} "
          f"(0 = cold start, 100% offline)")

    rlpd_train(
        base_policy_dir=base,
        offline=offline,
        online=online,
        n_updates=args.n_updates,
        utd_ratio=args.utd_ratio,
        batch_size=args.batch_size,
        online_ratio=args.online_ratio if len(online) else 0.0,
        out_dir=Path(args.out_dir),
        trace_family=args.trace_family,
        n_jobs=args.n_jobs,
        n_nodes=args.n_nodes,
        gpus_per_node=args.gpus_per_node,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
