"""Evaluate Hierarchical DSAC (D-LinUCB + DSAC) vs score vs PPO.

Runs HierarchicalTrainer once per (trace_family × seed), then reports
paired CI across seeds.

Usage:
    .venv-m11/bin/python eval/scripts/eval_hierarchical.py \
        --n-outer 5 --n-inner 500 \
        --seeds 42 43 44 45 46 \
        --trace-families philly burst ali \
        --n-jobs 200 --n-nodes 2 --gpus-per-node 2 \
        --out-csv eval/results/hierarchical_full.csv
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path
from typing import List, Tuple

import numpy as np

# Ensure repo root is on path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sim.loader import generate_by_family
from sim.runner import run as sim_run
from services.rl_scheduler.hierarchical import HierarchicalTrainer


def run_score(*, trace_family: str, n_jobs: int, seed: int,
              n_nodes: int, gpus_per_node: int) -> float:
    jobs = generate_by_family(trace_family, n_jobs=n_jobs, seed=seed)
    metrics, _ = sim_run(jobs, n_nodes=n_nodes, gpus_per_node=gpus_per_node,
                          scheduler_name="score")
    return float(metrics.summary()["jct_mean"])


def run_multifactor(*, trace_family: str, n_jobs: int, seed: int,
                    n_nodes: int, gpus_per_node: int) -> float:
    jobs = generate_by_family(trace_family, n_jobs=n_jobs, seed=seed)
    metrics, _ = sim_run(jobs, n_nodes=n_nodes, gpus_per_node=gpus_per_node,
                          scheduler_name="multifactor")
    return float(metrics.summary()["jct_mean"])


def run_hier(*, trace_family: str, n_jobs: int, seed: int,
             n_nodes: int, gpus_per_node: int,
             n_outer: int, n_inner: int, utd_ratio: int,
             batch_size: int, offline_buffer: int,
             out_base: Path) -> float:
    out_dir = out_base / f"{trace_family}_seed{seed}"
    trainer = HierarchicalTrainer(
        trace_family=trace_family,
        n_jobs=n_jobs,
        n_nodes=n_nodes,
        gpus_per_node=gpus_per_node,
        n_outer=n_outer,
        n_inner=n_inner,
        utd_ratio=utd_ratio,
        batch_size=batch_size,
        offline_buffer_size=offline_buffer,
        seed=seed,
        out_dir=out_dir,
    )
    result = trainer.train()
    return float(result["best_jct"])


def paired_ci(diffs: List[float],
              conf: float = 0.95) -> Tuple[float, float, float]:
    arr = np.asarray(diffs, dtype=np.float64)
    n = len(arr)
    mean = float(arr.mean())
    if n < 2:
        return mean, mean, mean
    sd = float(arr.std(ddof=1))
    from scipy import stats
    t_crit = float(stats.t.ppf(0.5 + conf / 2, df=n - 1))
    half = t_crit * sd / math.sqrt(n)
    return mean, mean - half, mean + half


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-outer", type=int, default=5)
    p.add_argument("--n-inner", type=int, default=500)
    p.add_argument("--utd-ratio", type=int, default=4)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--offline-buffer", type=int, default=3000)
    p.add_argument("--seeds", nargs="+", type=int, default=[42, 43, 44, 45, 46])
    p.add_argument("--trace-families", nargs="+", default=["philly", "burst", "ali"])
    p.add_argument("--n-jobs", type=int, default=200)
    p.add_argument("--n-nodes", type=int, default=2)
    p.add_argument("--gpus-per-node", type=int, default=2)
    p.add_argument("--out-csv",
                   default="eval/results/hierarchical_full.csv")
    p.add_argument("--out-base",
                   default=f"runs/hier_eval_{time.strftime('%Y%m%d-%H%M%S')}")
    args = p.parse_args(argv)

    out_base = Path(args.out_base)
    out_base.mkdir(parents=True, exist_ok=True)

    rows = []
    for fam in args.trace_families:
        print(f"\n{'='*55}")
        print(f"Trace family: {fam}")
        print(f"{'='*55}")
        for seed in args.seeds:
            t0 = time.time()
            kw = dict(trace_family=fam, n_jobs=args.n_jobs, seed=seed,
                      n_nodes=args.n_nodes, gpus_per_node=args.gpus_per_node)
            score_jct = run_score(**kw)
            mf_jct    = run_multifactor(**kw)
            hier_jct  = run_hier(
                **kw,
                n_outer=args.n_outer, n_inner=args.n_inner,
                utd_ratio=args.utd_ratio, batch_size=args.batch_size,
                offline_buffer=args.offline_buffer,
                out_base=out_base,
            )
            elapsed = time.time() - t0
            rows.append({
                "family": fam, "seed": seed,
                "score_jct": score_jct,
                "multifactor_jct": mf_jct,
                "hier_jct": hier_jct,
                "elapsed_s": elapsed,
            })
            print(f"  seed={seed:2d}  score={score_jct/3600:.3f}h  "
                  f"mf={mf_jct/3600:.3f}h  hier={hier_jct/3600:.3f}h  "
                  f"t={elapsed:.0f}s")

    # Write CSV
    Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_csv, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nCSV written: {args.out_csv}")

    # Paired CI table
    print(f"\n{'='*65}")
    print(f"{'Family':<8}  {'Δ(score−hier)':>16}  {'95% CI':>25}  sig")
    print(f"{'-'*65}")
    for fam in args.trace_families:
        fam_rows = [r for r in rows if r["family"] == fam]
        diffs = [r["score_jct"] - r["hier_jct"] for r in fam_rows]
        mean, lo, hi = paired_ci(diffs)
        sig = "***" if lo > 0 else ("   " if hi < 0 else "   ")
        pct = mean / np.mean([r["score_jct"] for r in fam_rows]) * 100
        print(f"{fam:<8}  {mean/3600:>+10.3f}h ({pct:+.1f}%)  "
              f"[{lo/3600:+.3f}, {hi/3600:+.3f}]h  {sig}")
    print(f"(positive = hierarchical DSAC better than score)\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
