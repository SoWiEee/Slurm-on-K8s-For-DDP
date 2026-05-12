"""Summarise an E7 sacct CSV into JCT / wait / makespan stats."""
from __future__ import annotations

import csv
import datetime as dt
import json
import math
import statistics
import sys


def parse_ts(s: str):
    if not s or s == "Unknown":
        return None
    return dt.datetime.fromisoformat(s.replace("T", " ")).timestamp()


def main(path: str) -> int:
    rows = []
    with open(path) as fh:
        for raw in csv.reader(fh, delimiter="|"):
            if not raw or not raw[0]:
                continue
            jid, name, sub, start, end, elapsed, state = raw[:7]
            ts_sub, ts_start, ts_end = parse_ts(sub), parse_ts(start), parse_ts(end)
            if None in (ts_sub, ts_start, ts_end):
                continue
            rows.append({
                "job_id": jid, "name": name,
                "submit": ts_sub, "start": ts_start, "end": ts_end,
                "wait": ts_start - ts_sub,
                "jct": ts_end - ts_sub,
                "state": state,
            })
    if not rows:
        json.dump({"n_jobs": 0}, sys.stdout, indent=2)
        return 0
    jcts = [r["jct"] for r in rows]
    waits = [r["wait"] for r in rows]
    out = {
        "n_jobs": len(rows),
        "makespan_sec": max(r["end"] for r in rows) - min(r["submit"] for r in rows),
        "jct_mean": statistics.fmean(jcts),
        "jct_p50": statistics.quantiles(jcts, n=100)[49] if len(jcts) > 1 else jcts[0],
        "jct_p90": statistics.quantiles(jcts, n=100)[89] if len(jcts) > 1 else jcts[0],
        "jct_p95": statistics.quantiles(jcts, n=100)[94] if len(jcts) > 1 else jcts[0],
        "wait_mean": statistics.fmean(waits),
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
