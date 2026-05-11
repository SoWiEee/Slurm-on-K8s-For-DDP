"""Print compact summary tables for the eval-writeup.

Reads ``eval/results/agg_by_run.json`` (per (trace, exp, run-config))
and ``eval/results/all_summaries.json`` (per-seed flat). Prints:

  1. Per-trace main table (mean ± std across seeds)
  2. Paired same-seed diffs per trace (E4-E2, E5-E4, E5-E2, E5b-E5)
  3. Cross-trace generalisation: does the headline result hold on all traces?
  4. E6 sensitivity (mean across seeds, per trace)
"""
import json
import math
import os
import statistics
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
AGG = os.path.join(ROOT, "eval/results/agg_by_run.json")
FLAT = os.path.join(ROOT, "eval/results/all_summaries.json")

T_CRIT_95 = {2: 12.706, 3: 4.303, 4: 3.182, 5: 2.776, 6: 2.571,
             7: 2.447, 8: 2.365, 9: 2.306, 10: 2.262}

HEADLINE_EXPS = ("e1", "e2", "e3", "e4", "e5", "e5b")


def _paired_diff(flat, trace: str, a_exp: str, b_exp: str):
    """Return (n, mean_rel_pct, ci95_rel_pct, mean_abs_h, ci95_abs_h)."""
    a = {r["synth_seed"]: r for r in flat
         if r["experiment"] == a_exp and r.get("trace_family") == trace}
    b = {r["synth_seed"]: r for r in flat
         if r["experiment"] == b_exp and r.get("trace_family") == trace}
    common = sorted(set(a) & set(b))
    if not common:
        return None
    rel = [(a[s]["jct_mean"] / b[s]["jct_mean"] - 1.0) for s in common]
    abs_h = [(a[s]["jct_mean"] - b[s]["jct_mean"]) / 3600.0 for s in common]
    n = len(rel)
    mean_rel = statistics.fmean(rel) * 100.0
    mean_abs = statistics.fmean(abs_h)
    if n > 1:
        ci_rel = T_CRIT_95.get(n, 1.96) * statistics.stdev(rel) * 100.0 / math.sqrt(n)
        ci_abs = T_CRIT_95.get(n, 1.96) * statistics.stdev(abs_h) / math.sqrt(n)
    else:
        ci_rel = ci_abs = 0.0
    return n, mean_rel, ci_rel, mean_abs, ci_abs


def _significance(mean: float, ci: float) -> str:
    if abs(mean) <= ci:
        return "—"  # not significant
    return "↓" if mean < 0 else "↑"


def print_per_trace(rows, flat, trace: str):
    print(f"\n### Trace: {trace}")
    print(f"Main table (n_seeds reflects seeds run for this trace; numbers are mean ± std)")
    hdr = ("exp", "n", "jct_mean(h)±std", "ci95(h)",
           "jct_p90(h)", "slow_mean", "util", "bf_rate", "requeue/run", "ckpt_cost(h)")
    print("|".join(f"{x:>18}" for x in hdr))
    print("-" * (19 * len(hdr)))
    for s in rows:
        if s["trace_family"] != trace or s["experiment"] not in HEADLINE_EXPS:
            continue
        cols = (
            s["experiment"], s["n_seeds"],
            f"{s['jct_mean_mean']/3600:.3f}±{s['jct_mean_std']/3600:.3f}",
            f"±{s['jct_mean_ci95']/3600:.3f}",
            f"{s['jct_p90_mean']/3600:.3f}",
            f"{s['slowdown_mean_mean']:.3f}",
            f"{s['utilization_mean']:.3f}",
            f"{s['bf_rate_mean']:.3f}",
            f"{s.get('requeue_count_mean', 0):.0f}",
            f"{s.get('requeue_cost_total_mean', 0)/3600:.3f}",
        )
        print("|".join(f"{x:>18}" for x in cols))

    print("\nPaired same-seed diffs:")
    for a, b in (("e4", "e2"), ("e5", "e4"), ("e5", "e2"), ("e5b", "e5")):
        r = _paired_diff(flat, trace, a, b)
        if r is None:
            continue
        n, m_rel, ci_rel, m_abs, ci_abs = r
        sig = _significance(m_rel, ci_rel)
        print(f"  {a} − {b}: {m_rel:+7.2f}% (±{ci_rel:.2f}%)  "
              f"Δ={m_abs:+.3f}h (±{ci_abs:.3f}h)  n={n}  {sig}")


def cross_trace_summary(rows, flat):
    """Did the M5 predictor win (E4 vs E2) and the M7 regression (E5 vs E4)
    generalise across all traces? Print one consolidated table."""
    traces = sorted({r["trace_family"] for r in rows
                     if r.get("trace_family")})
    print("\n## Cross-trace generalisation summary")
    print(f"Traces evaluated: {', '.join(traces)}")
    print()
    hdr = ("trace", "E4-E2 paired Δ%", "ci95", "sig",
           "E5-E4 paired Δ%", "ci95", "sig")
    print("|".join(f"{x:>16}" for x in hdr))
    print("-" * (17 * len(hdr)))
    for trace in traces:
        r1 = _paired_diff(flat, trace, "e4", "e2")
        r2 = _paired_diff(flat, trace, "e5", "e4")
        cells = [trace]
        for r in (r1, r2):
            if r is None:
                cells.extend(["–", "–", "–"])
            else:
                n, m, ci, _a, _b = r
                cells.append(f"{m:+.2f}")
                cells.append(f"±{ci:.2f}")
                cells.append(_significance(m, ci))
        print("|".join(f"{x:>16}" for x in cells))


def print_e6_sensitivity(rows):
    by_trace = defaultdict(list)
    for s in rows:
        if s["experiment"] == "e6":
            by_trace[s["trace_family"]].append(s)
    for trace, items in sorted(by_trace.items()):
        if not items:
            continue
        print(f"\n## E6 sensitivity — trace={trace} (mean jct in hours across seeds)")
        alphas = sorted({float(s["alpha"]) for s in items})
        deltas = sorted({float(s["delta"]) for s in items})
        hdr = ["α \\ δ"] + [f"{d:.2f}" for d in deltas]
        print("|".join(f"{x:>10}" for x in hdr))
        print("-" * (11 * len(hdr)))
        by_ad = {(float(s["alpha"]), float(s["delta"])): s for s in items}
        best = min(items, key=lambda s: s["jct_mean_mean"])
        worst = max(items, key=lambda s: s["jct_mean_mean"])
        for a in alphas:
            row = [f"{a:.2f}"]
            for d in deltas:
                s = by_ad.get((a, d))
                if s is None:
                    row.append("—")
                else:
                    v = s["jct_mean_mean"] / 3600
                    marker = ""
                    if s is best:
                        marker = " *"
                    elif s is worst:
                        marker = " ✗"
                    row.append(f"{v:.2f}{marker}")
            print("|".join(f"{x:>10}" for x in row))
        rng = (worst["jct_mean_mean"] - best["jct_mean_mean"]) / best["jct_mean_mean"]
        print(f"  best * = α={best['alpha']} δ={best['delta']} ({best['jct_mean_mean']/3600:.3f}h); "
              f"worst ✗ = α={worst['alpha']} δ={worst['delta']} ({worst['jct_mean_mean']/3600:.3f}h); "
              f"spread = {rng*100:.1f}%")


def main():
    if not os.path.exists(AGG):
        print(f"missing {AGG}; run eval/scripts/run_all.sh first")
        return
    with open(AGG) as fh:
        rows = json.load(fh)
    with open(FLAT) as fh:
        flat = json.load(fh)

    traces = sorted({r["trace_family"] for r in rows
                     if r.get("trace_family") and r.get("experiment") in HEADLINE_EXPS})
    for t in traces:
        print_per_trace(rows, flat, t)

    cross_trace_summary(rows, flat)
    print_e6_sensitivity(rows)


if __name__ == "__main__":
    main()
