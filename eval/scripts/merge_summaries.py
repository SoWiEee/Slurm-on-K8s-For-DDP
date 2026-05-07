"""Merge per-experiment summary JSONs into one file for plotting.

Output: ``<results_dir>/all_summaries.json`` — list of dicts, each carrying
``experiment``, ``run`` and the runner's summary fields.
"""
from __future__ import annotations

import json
import os
import sys


def main(results_dir: str) -> int:
    out = []
    for exp in sorted(os.listdir(results_dir)):
        exp_dir = os.path.join(results_dir, exp)
        if not os.path.isdir(exp_dir):
            continue
        for fn in sorted(os.listdir(exp_dir)):
            if not fn.endswith(".json"):
                continue
            with open(os.path.join(exp_dir, fn)) as fh:
                d = json.load(fh)
            d["experiment"] = exp
            d["run"] = os.path.splitext(fn)[0]
            out.append(d)
    target = os.path.join(results_dir, "all_summaries.json")
    with open(target, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"  merged {len(out)} runs → {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "eval/results"))
