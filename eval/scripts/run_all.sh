#!/usr/bin/env bash
# eval/scripts/run_all.sh — Phase 6 M8 experiment driver.
#
# E1..E6 use the offline simulator (sim/runner.py) on a Philly-derived
# 1000-job trace. E7 is documented separately in run_e7_live.sh — it
# needs an actual k3s/Slurm cluster.
#
# Output layout:
#   eval/results/<exp>/<run>.csv      per-job records
#   eval/results/<exp>/<run>.json     summary metrics
#   eval/results/<exp>.summary.json   merged summaries for plotting
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PY=${PY:-.venv-m5/bin/python}
TRACE=${TRACE:-sim/data/philly_subsample.json}
NODES=${NODES:-4}
GPN=${GPN:-4}
OUT="$ROOT/eval/results"
mkdir -p "$OUT"

run_sim() {
  # run_sim <experiment> <run-name> <extra runner args...>
  local exp="$1"; shift
  local name="$1"; shift
  mkdir -p "$OUT/$exp"
  "$PY" -m sim.runner \
      --trace "$TRACE" \
      --nodes "$NODES" --gpus-per-node "$GPN" \
      --output "$OUT/$exp/$name.csv" \
      --summary-json "$OUT/$exp/$name.json" \
      "$@" >/dev/null
  echo "  $exp/$name OK"
}

step() { printf "\n[eval] %s\n" "$*"; }

# E1 — baseline FCFS (no backfill)
step "E1 baseline (FCFS, no backfill)"
run_sim e1 fcfs --scheduler fcfs

# E2 — Slurm vendor default (multifactor + backfill)
step "E2 vendor (multifactor + backfill)"
run_sim e2 multifactor --scheduler multifactor

# E3 — our v0 (M3 score, no predictor, no fragmentation)
step "E3 our-v0 (M3 score; epsilon=0)"
run_sim e3 score-m3 --scheduler score --alpha 0.40 --beta 0.20 --delta 0.20 --epsilon 0.0

# E4 — our v1 (M3 score + M5 predictor; epsilon=0.30)
step "E4 our-v1 (M3 score + M5 predictor; epsilon=0.30)"
run_sim e4 score-m5 --scheduler score --alpha 0.40 --beta 0.20 --delta 0.20 --epsilon 0.30

# E5 — our v2 (E4 + M7 fragmentation requeue)
step "E5 our-v2 (E4 + M7 fragmentation reconciler)"
run_sim e5 score-m7 --scheduler score --alpha 0.40 --beta 0.20 --delta 0.20 --epsilon 0.30 --fragmentation

# E6 — sensitivity grid (9 combos around E4 weights)
step "E6 sensitivity grid"
i=0
for a in 0.20 0.40 0.60; do
  for d in 0.10 0.20 0.30; do
    name=$(printf "a%s_d%s" "$a" "$d")
    run_sim e6 "$name" --scheduler score --alpha "$a" --beta 0.20 --delta "$d" --epsilon 0.30
    i=$((i+1))
  done
done
echo "  E6: $i combos"

# Aggregate summaries for plotting
step "merging summaries"
"$PY" "$ROOT/eval/scripts/merge_summaries.py" "$OUT"

step "done"
