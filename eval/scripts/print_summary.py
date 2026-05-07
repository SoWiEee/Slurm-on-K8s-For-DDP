"""Print a compact summary table for the eval-writeup."""
import json
import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
data = json.load(open(os.path.join(ROOT, "eval/results/all_summaries.json")))

hdr = ("exp", "run", "jct_mean(h)", "jct_p90(h)", "jct_p95(h)",
       "slow_mean", "util", "bf_rate", "requeue")
print("|".join(f"{x:>14}" for x in hdr))
print("-" * (15 * len(hdr)))
for s in data:
    if s["experiment"] in ("e1", "e2", "e3", "e4", "e5"):
        cols = (
            s["experiment"], s["run"],
            f"{s['jct_mean']/3600:.3f}",
            f"{s['jct_p90']/3600:.3f}",
            f"{s['jct_p95']/3600:.3f}",
            f"{s['slowdown_mean']:.3f}",
            f"{s['utilization']:.3f}",
            f"{s['bf_rate']:.3f}",
            str(s.get("requeue_count", 0)),
        )
        print("|".join(f"{x:>14}" for x in cols))

print()
print("E6 sensitivity:")
for s in sorted([s for s in data if s["experiment"] == "e6"],
                key=lambda r: (r.get("alpha", 0), r.get("delta", 0))):
    print(f"  alpha={s.get('alpha')} delta={s.get('delta')}  "
          f"jct_mean={s['jct_mean']/3600:.3f}h  "
          f"slow_mean={s['slowdown_mean']:.3f}  "
          f"util={s['utilization']:.3f}")
