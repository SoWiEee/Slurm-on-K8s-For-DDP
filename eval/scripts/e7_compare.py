"""Compare two E7 passes (vendor.csv vs our.csv).

Each row from `dump_sacct` is:
  JobID|JobName|Submit|Start|End|Elapsed|State

Job names are tag-N-{s,m,l}. We pair by the trailing "N-{s,m,l}" suffix
(stripping the tag prefix), so the same logical job — e.g. job index 7
of class "small" — gets compared across passes. Submit/Start/End are
ISO timestamps from sacct.
"""
from __future__ import annotations

import csv
import math
import statistics
import sys
from datetime import datetime


def parse_ts(s: str):
    if not s or s in ("Unknown", "None"):
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def load(path: str):
    """Return {key: {jct, wait, runtime, state}} keyed by N-class suffix."""
    out = {}
    with open(path) as fh:
        for row in csv.reader(fh, delimiter="|"):
            if len(row) < 7:
                continue
            jobid, name, submit, start, end, elapsed, state = row[:7]
            if "-" not in name:
                continue
            suffix = name.split("-", 1)[1]  # drop "vendor-" / "our-" prefix
            ts_sub = parse_ts(submit); ts_st = parse_ts(start); ts_en = parse_ts(end)
            if ts_sub is None or ts_en is None:
                continue
            jct = (ts_en - ts_sub).total_seconds()
            wait = (ts_st - ts_sub).total_seconds() if ts_st else None
            run = (ts_en - ts_st).total_seconds() if ts_st else None
            out[suffix] = {"jct": jct, "wait": wait, "runtime": run,
                           "state": state, "jobid": jobid}
    return out


def pct(xs, p):
    if not xs:
        return 0.0
    s = sorted(xs)
    k = (len(s) - 1) * p / 100.0
    f = math.floor(k); c = math.ceil(k)
    return s[int(k)] if f == c else s[f] + (s[c] - s[f]) * (k - f)


def summarise(label, recs):
    jcts = [r["jct"] for r in recs.values()]
    waits = [r["wait"] for r in recs.values() if r["wait"] is not None]
    runs = [r["runtime"] for r in recs.values() if r["runtime"] is not None]
    if not jcts:
        print(f"  {label}: no records")
        return
    print(f"  {label}: n={len(jcts)}")
    print(f"    JCT  mean={statistics.fmean(jcts):.1f}s  p90={pct(jcts,90):.1f}s  max={max(jcts):.1f}s")
    print(f"    wait mean={statistics.fmean(waits):.1f}s  p90={pct(waits,90):.1f}s")
    print(f"    runtime mean={statistics.fmean(runs):.1f}s")


def main(vendor_csv: str, our_csv: str) -> int:
    v = load(vendor_csv)
    o = load(our_csv)
    print(f"== E7 paired comparison ==")
    print(f"vendor.csv: {vendor_csv}  rows={len(v)}")
    print(f"   our.csv: {our_csv}  rows={len(o)}")
    print()
    print("Per-pass summary:")
    summarise("vendor", v)
    summarise("our   ", o)

    common = sorted(set(v) & set(o))
    print(f"\nPaired by job suffix — n={len(common)} common jobs")
    if not common:
        print("no paired jobs; check job-name tagging")
        return 1
    djct = [o[k]["jct"] - v[k]["jct"] for k in common]
    dwait = [o[k]["wait"] - v[k]["wait"] for k in common
             if v[k]["wait"] is not None and o[k]["wait"] is not None]
    mean_djct = statistics.fmean(djct)
    rel = mean_djct / statistics.fmean([v[k]["jct"] for k in common]) * 100
    print(f"  Δ JCT  (our − vendor): mean={mean_djct:+.1f}s "
          f"({rel:+.2f}%)  p90={pct(djct,90):+.1f}s")
    if dwait:
        print(f"  Δ wait (our − vendor): mean={statistics.fmean(dwait):+.1f}s  "
              f"p90={pct(dwait,90):+.1f}s")
    # Paired sign-rank: count how many jobs saw our < vendor
    better = sum(1 for d in djct if d < 0)
    print(f"  {better}/{len(djct)} jobs had lower JCT under 'our' "
          f"({better/len(djct)*100:.0f}%)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: e7_compare.py vendor.csv our.csv", file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
