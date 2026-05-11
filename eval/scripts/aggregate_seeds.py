"""Aggregate per-seed summaries into mean / std / 95% CI per run config.

Reads ``eval/results/all_summaries.json`` (flat list of per-seed runs)
and writes ``eval/results/agg_by_run.json`` — one row per (experiment,
run-config-key), with statistics across seeds.

A "run config" is identified by the run name *without* the seed suffix
(we stripped ``__seedNN`` off, so different seeds collapse into the same
config). For each numeric summary field we report mean, std (sample),
n, and a 95% confidence interval based on the Student-t distribution.
"""
from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
from collections import defaultdict
from typing import Dict, List

# Student-t two-sided 95% critical values for small n (n-1 dof).
# n=1 → inf (CI undefined, we just report mean). n=2..10 hard-coded.
T_CRIT_95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
             7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}

NUMERIC_FIELDS = (
    "jct_mean", "jct_p50", "jct_p90", "jct_p95",
    "wait_mean", "wait_p90",
    "slowdown_mean", "slowdown_p90",
    "utilization", "bf_rate",
    "requeue_count", "requeue_cost_total",
    "makespan",
)

SEED_RE = re.compile(r"__seed\d+$")


def _strip_seed(run: str) -> str:
    return SEED_RE.sub("", run)


def _ci95(values: List[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    sd = statistics.stdev(values)
    sem = sd / math.sqrt(n)
    t = T_CRIT_95.get(n, 1.96)  # fall back to z for big n
    return t * sem


def aggregate(summaries: List[dict]) -> List[dict]:
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for s in summaries:
        key = (s.get("trace_family", "philly"),
               s.get("experiment"),
               _strip_seed(s.get("run", "")))
        groups[key].append(s)

    rows = []
    for (trace, exp, run), group in sorted(groups.items()):
        if not group:
            continue
        head = group[0]
        row = {
            "trace_family": trace,
            "experiment": exp,
            "run": run,
            "n_seeds": len(group),
            "scheduler": head.get("scheduler"),
            "fragmentation": head.get("fragmentation"),
            "ckpt_reload_cost": head.get("ckpt_reload_cost"),
        }
        for k in ("alpha", "beta", "delta", "epsilon"):
            if k in head:
                row[k] = head[k]
        for field in NUMERIC_FIELDS:
            vals = [g[field] for g in group if field in g and g[field] is not None]
            if not vals:
                continue
            row[f"{field}_mean"] = statistics.fmean(vals)
            row[f"{field}_std"] = statistics.stdev(vals) if len(vals) > 1 else 0.0
            row[f"{field}_ci95"] = _ci95(vals)
            row[f"{field}_min"] = min(vals)
            row[f"{field}_max"] = max(vals)
        row["seeds"] = sorted([g.get("synth_seed") for g in group
                               if g.get("synth_seed") is not None])
        rows.append(row)
    return rows


def main(results_dir: str) -> int:
    src = os.path.join(results_dir, "all_summaries.json")
    with open(src) as fh:
        summaries = json.load(fh)
    rows = aggregate(summaries)
    target = os.path.join(results_dir, "agg_by_run.json")
    with open(target, "w") as fh:
        json.dump(rows, fh, indent=2)
    print(f"  aggregated {len(summaries)} runs into {len(rows)} configs → {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "eval/results"))
