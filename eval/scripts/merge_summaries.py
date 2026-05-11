"""Merge per-experiment summary JSONs into one file for plotting.

Scans both layouts:
  <results>/<exp>/<run>.json                    (legacy single-trace)
  <results>/<trace>/<exp>/<run>__seed<N>.json   (multi-trace)

Output: ``<results>/all_summaries.json`` — list of dicts, each carrying
``trace_family``, ``experiment``, ``run`` and the runner's summary fields.
"""
from __future__ import annotations

import json
import os
import sys

TRACE_FAMILIES = ("philly", "burst", "ali")
SKIP_AT_ROOT = {"all_summaries.json", "agg_by_run.json"}


def _collect(d: str, trace_family: str, out: list) -> None:
    if not os.path.isdir(d):
        return
    for exp in sorted(os.listdir(d)):
        exp_dir = os.path.join(d, exp)
        if not os.path.isdir(exp_dir):
            continue
        for fn in sorted(os.listdir(exp_dir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(exp_dir, fn)) as fh:
                rec = json.load(fh)
            rec.setdefault("trace_family", trace_family)
            rec["experiment"] = exp
            rec["run"] = os.path.splitext(fn)[0]
            out.append(rec)


def main(results_dir: str) -> int:
    out: list = []
    # Multi-trace nested layout
    for trace in TRACE_FAMILIES:
        _collect(os.path.join(results_dir, trace), trace, out)
    # Legacy flat layout (no trace subdir): treat as 'philly'
    if not any(os.path.isdir(os.path.join(results_dir, t)) for t in TRACE_FAMILIES):
        _collect(results_dir, "philly", out)
    target = os.path.join(results_dir, "all_summaries.json")
    with open(target, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"  merged {len(out)} runs → {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "eval/results"))
