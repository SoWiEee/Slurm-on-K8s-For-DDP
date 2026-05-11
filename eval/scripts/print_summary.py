"""Print a compact summary table for the eval-writeup.

Reads ``eval/results/agg_by_run.json`` (produced by aggregate_seeds.py)
and prints mean ± std (over N seeds), plus a 95% CI half-width on the
key claim (jct_mean). Falls back to ``all_summaries.json`` if the
aggregate file is missing (single-seed legacy mode).
"""
import json
import math
import os
import statistics

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AGG = os.path.join(ROOT, "eval/results/agg_by_run.json")
FLAT = os.path.join(ROOT, "eval/results/all_summaries.json")

T_CRIT_95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
             7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}


def _print_paired_diff(a_exp: str, b_exp: str) -> None:
    """Paired comparison: (a - b) per seed, then mean ± CI of diffs.
    Uses synth_seed to pair runs; the same seed → same trace, so the
    only variable is the scheduler. Much tighter than unpaired CIs.
    """
    with open(FLAT) as fh:
        flat = json.load(fh)
    a_by_seed = {r["synth_seed"]: r for r in flat if r["experiment"] == a_exp}
    b_by_seed = {r["synth_seed"]: r for r in flat if r["experiment"] == b_exp}
    common = sorted(set(a_by_seed) & set(b_by_seed))
    if not common:
        return
    diffs = [(a_by_seed[s]["jct_mean"] - b_by_seed[s]["jct_mean"]) for s in common]
    rel = [(a_by_seed[s]["jct_mean"] / b_by_seed[s]["jct_mean"] - 1.0) for s in common]
    n = len(diffs)
    mean_diff = statistics.fmean(diffs) / 3600.0
    mean_rel = statistics.fmean(rel) * 100.0
    if n > 1:
        sd = statistics.stdev(diffs) / 3600.0
        ci = T_CRIT_95.get(n, 1.96) * sd / math.sqrt(n)
        sd_r = statistics.stdev(rel) * 100.0
        ci_r = T_CRIT_95.get(n, 1.96) * sd_r / math.sqrt(n)
    else:
        ci = ci_r = 0.0
    direction = "worse" if mean_diff > 0 else "better"
    print(f"  {a_exp:>4s} − {b_exp:<4s}: Δjct_mean = {mean_diff:+.3f}h "
          f"(95% CI ±{ci:.3f}h, n={n})  →  {mean_rel:+.2f}% "
          f"(±{ci_r:.2f}%) — {a_exp} is {direction} than {b_exp}")


def fmt(v):
    return f"{v:.3f}" if isinstance(v, float) else str(v)


def print_aggregated(rows):
    headline_exps = ("e1", "e2", "e3", "e4", "e5", "e5b")
    hdr = ("exp", "run", "n",
           "jct_mean(h)±std", "jct_mean ±ci95",
           "jct_p90(h)±std", "slow_mean", "util", "bf_rate", "requeue", "ckpt_cost(h)")
    print("|".join(f"{x:>22}" for x in hdr))
    print("-" * (23 * len(hdr)))
    for s in rows:
        if s["experiment"] not in headline_exps:
            continue
        cols = (
            s["experiment"], s["run"], s["n_seeds"],
            f"{s['jct_mean_mean']/3600:.3f}±{s['jct_mean_std']/3600:.3f}",
            f"±{s['jct_mean_ci95']/3600:.3f}",
            f"{s['jct_p90_mean']/3600:.3f}±{s['jct_p90_std']/3600:.3f}",
            f"{s['slowdown_mean_mean']:.3f}",
            f"{s['utilization_mean']:.3f}",
            f"{s['bf_rate_mean']:.3f}",
            f"{s.get('requeue_count_mean', 0):.0f}",
            f"{s.get('requeue_cost_total_mean', 0)/3600:.3f}",
        )
        print("|".join(f"{x:>22}" for x in cols))

    print()
    print("Paired same-seed comparisons (much tighter than unpaired CIs):")
    _print_paired_diff("e5", "e2")
    _print_paired_diff("e4", "e2")
    _print_paired_diff("e5", "e4")
    _print_paired_diff("e5b", "e5")

    # E6 sensitivity (averaged over seeds)
    print()
    print("E6 sensitivity (mean across seeds):")
    e6 = [s for s in rows if s["experiment"] == "e6"]
    for s in sorted(e6, key=lambda r: (r.get("alpha", 0), r.get("delta", 0))):
        print(f"  alpha={s.get('alpha')} delta={s.get('delta')}  "
              f"jct_mean={s['jct_mean_mean']/3600:.3f}±{s['jct_mean_std']/3600:.3f}h  "
              f"util={s['utilization_mean']:.3f}")


def main():
    if os.path.exists(AGG):
        with open(AGG) as fh:
            print_aggregated(json.load(fh))
        return
    # Legacy single-seed fallback.
    print("warning: agg_by_run.json missing; falling back to single-seed view")
    with open(FLAT) as fh:
        data = json.load(fh)
    hdr = ("exp", "run", "jct_mean(h)", "jct_p90(h)", "util", "requeue")
    print("|".join(f"{x:>14}" for x in hdr))
    for s in data:
        if s["experiment"] in ("e1", "e2", "e3", "e4", "e5", "e5b"):
            print("|".join(f"{x:>14}" for x in (
                s["experiment"], s["run"],
                f"{s['jct_mean']/3600:.3f}", f"{s['jct_p90']/3600:.3f}",
                f"{s['utilization']:.3f}", str(s.get("requeue_count", 0)))))


if __name__ == "__main__":
    main()
