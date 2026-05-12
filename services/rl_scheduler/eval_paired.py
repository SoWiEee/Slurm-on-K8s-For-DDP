"""M11 Phase B-3: paired-CI evaluation of trained PPO vs heuristic baselines.

For each (trace_family, seed), runs:
- fcfs, multifactor, score (via sim.runner)
- ppo (via sim.gym_env + saved policy)

with identical synthetic trace (same seed = same jobs) and computes
paired differences in avg_jct. Reports 95% CI on the diffs.

Run:
    .venv-m11/bin/python -m services.rl_scheduler.eval_paired \\
        --policy-dir runs/m11_ppo_20260512-130000 \\
        --seeds 42 43 44 45 46 \\
        --trace-families philly burst ali \\
        --n-jobs 200
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import torch
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

try:
    from sb3_contrib import MaskablePPO
except ImportError:
    MaskablePPO = None  # type: ignore

from sim.gym_env import KubefluxSchedGymEnv
from sim.loader import generate_by_family
from sim.runner import run


def run_baseline(scheduler_name: str, *, trace_family: str, n_jobs: int,
                 seed: int, n_nodes: int, gpus_per_node: int) -> float:
    jobs = generate_by_family(trace_family, n_jobs=n_jobs, seed=seed)
    metrics, _ = run(
        jobs,
        n_nodes=n_nodes,
        gpus_per_node=gpus_per_node,
        scheduler_name=scheduler_name,
    )
    return float(metrics.summary()["jct_mean"])


def run_ppo(model, vecnorm_path: str, *, trace_family: str, n_jobs: int,
            seed: int, n_nodes: int, gpus_per_node: int,
            masked: bool = False) -> float:
    def _factory():
        return generate_by_family(trace_family, n_jobs=n_jobs, seed=seed)
    raw_env = KubefluxSchedGymEnv(
        jobs_factory=_factory,
        n_nodes=n_nodes,
        gpus_per_node=gpus_per_node,
        max_steps=n_jobs * 100,
    )
    venv = DummyVecEnv([lambda: raw_env])
    venv = VecNormalize.load(vecnorm_path, venv)
    venv.training = False
    venv.norm_reward = False

    obs = venv.reset()
    terminated = False
    info_buf: dict = {}
    steps = 0
    max_steps = n_jobs * 100
    while not terminated and steps < max_steps:
        if masked:
            mask = raw_env.action_masks()[np.newaxis, :]
            action, _ = model.predict(obs, action_masks=mask, deterministic=True)
        else:
            action, _ = model.predict(obs, deterministic=True)
        obs, _r, dones, infos = venv.step(action)
        terminated = bool(dones[0])
        info_buf = infos[0]
        steps += 1
    return float(info_buf.get("avg_jct", float("nan")))


def paired_ci(diffs: List[float], conf: float = 0.95) -> Tuple[float, float, float]:
    """Return (mean, lo, hi) for paired-CI on diffs."""
    diffs = np.asarray(diffs, dtype=np.float64)
    n = len(diffs)
    mean = float(diffs.mean())
    if n < 2:
        return mean, mean, mean
    sd = float(diffs.std(ddof=1))
    # Student-t critical at 95% two-sided with df=n-1
    from scipy import stats
    t_crit = float(stats.t.ppf(0.5 + conf / 2, df=n - 1))
    half = t_crit * sd / math.sqrt(n)
    return mean, mean - half, mean + half


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--policy-dir", required=True,
                   help="dir containing policy.zip + vecnormalize.pkl")
    p.add_argument("--seeds", type=int, nargs="+", default=[42, 43, 44, 45, 46])
    p.add_argument("--trace-families", nargs="+", default=["philly", "burst", "ali"])
    p.add_argument("--n-jobs", type=int, default=200)
    p.add_argument("--n-nodes", type=int, default=4)
    p.add_argument("--gpus-per-node", type=int, default=4)
    p.add_argument("--out-csv", default=None)
    args = p.parse_args(argv)

    pd = Path(args.policy_dir)
    policy_path = pd / "policy.zip"
    if not policy_path.exists():
        policy_path = pd / "policy"  # SB3 sometimes drops .zip
    vecnorm_path = pd / "vecnormalize.pkl"
    if not policy_path.exists() or not vecnorm_path.exists():
        print(f"ERROR: policy/vecnormalize missing under {pd}", file=sys.stderr)
        return 2

    masked = (pd / "MASKED").exists()
    if masked:
        if MaskablePPO is None:
            print("ERROR: MASKED marker present but sb3-contrib not installed",
                  file=sys.stderr)
            return 3
        device = "cpu"
        model = MaskablePPO.load(str(policy_path), device=device)
        print(f"loaded MaskablePPO from {policy_path}, device={device}")
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = PPO.load(str(policy_path), device=device)
        print(f"loaded PPO from {policy_path}, device={device}")

    SCHEDULERS = ["fcfs", "multifactor", "score", "ppo"]
    rows: List[Dict] = []
    for fam in args.trace_families:
        for seed in args.seeds:
            jct = {}
            for sname in SCHEDULERS:
                if sname == "ppo":
                    j = run_ppo(model, str(vecnorm_path),
                                trace_family=fam, n_jobs=args.n_jobs,
                                seed=seed, n_nodes=args.n_nodes,
                                gpus_per_node=args.gpus_per_node,
                                masked=masked)
                else:
                    j = run_baseline(sname, trace_family=fam,
                                     n_jobs=args.n_jobs, seed=seed,
                                     n_nodes=args.n_nodes,
                                     gpus_per_node=args.gpus_per_node)
                jct[sname] = j
                print(f"  {fam:7s} seed={seed:>4d}  {sname:11s}  jct={j:8.1f}")
            rows.append({"family": fam, "seed": seed, **jct})

    if args.out_csv:
        Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["family", "seed", *SCHEDULERS])
            w.writeheader()
            w.writerows(rows)
        print(f"\nwrote {args.out_csv}")

    # Paired CI per (family, baseline) on (baseline - ppo)
    print("\n=== Paired CI: avg_jct(baseline) − avg_jct(ppo), positive = ppo wins ===\n")
    for fam in args.trace_families:
        print(f"[{fam}]")
        for baseline in ["fcfs", "multifactor", "score"]:
            diffs = [r[baseline] - r["ppo"] for r in rows if r["family"] == fam]
            mean, lo, hi = paired_ci(diffs)
            sig = "***" if (lo > 0 or hi < 0) else "   "
            print(f"  {baseline:12s} − ppo  Δ={mean:+8.1f}  [{lo:+8.1f}, {hi:+8.1f}]  {sig}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
