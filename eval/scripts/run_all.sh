#!/usr/bin/env bash
# eval/scripts/run_all.sh — Phase 6 M8 experiment driver.
#
# E1..E6 use the offline simulator (sim/runner.py) on a Philly-like 1000-
# job synthetic trace. Each experiment runs across N seeds so we can
# report mean ± std and a 95% CI; the per-trace stochasticity comes from
# generate_philly_like (--synth-jobs + --synth-seed). E7 is documented
# separately in run_e7_live.sh — it needs an actual k3s/Slurm cluster.
#
# Output layout:
#   eval/results/<exp>/<run>__seed<N>.csv      per-job records
#   eval/results/<exp>/<run>__seed<N>.json     summary metrics
#   eval/results/all_summaries.json            merged summaries (one row per run × seed)
#   eval/results/agg_by_run.json               mean / std / 95% CI per (exp, run)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

PY=${PY:-.venv-m5/bin/python}
NODES=${NODES:-4}
GPN=${GPN:-4}
SYNTH_JOBS=${SYNTH_JOBS:-1000}
SEEDS=${SEEDS:-"42 43 44 45 46"}
TRACES=${TRACES:-"philly burst ali"}
# checkpoint reload cost (s) charged per requeue under fragmentation.
# Default 60s ≈ a realistic small-LLM warmup (DDP MNIST ≈ 10s, larger
# models can be several minutes — sweep externally if needed).
CKPT_COST=${CKPT_COST:-60.0}
# E6 sensitivity grid: SMALL = 3x3 (legacy), DENSE = 5x5 (fine grid)
E6_GRID=${E6_GRID:-DENSE}
OUT="$ROOT/eval/results"
mkdir -p "$OUT"

# Per-trace output prefix so seeds/configs don't collide across families.
trace_outdir() { echo "$OUT/$1"; }

run_sim_one_seed() {
  # run_sim_one_seed <trace> <experiment> <run-name> <seed> <extra runner args...>
  local trace="$1"; shift
  local exp="$1"; shift
  local name="$1"; shift
  local seed="$1"; shift
  local dir="$(trace_outdir "$trace")/$exp"
  mkdir -p "$dir"
  "$PY" -m sim.runner \
      --trace-family "$trace" \
      --synth-jobs "$SYNTH_JOBS" --synth-seed "$seed" \
      --nodes "$NODES" --gpus-per-node "$GPN" \
      --output "$dir/${name}__seed${seed}.csv" \
      --summary-json "$dir/${name}__seed${seed}.json" \
      "$@" >/dev/null
}

run_sim() {
  # run_sim <experiment> <run-name> <extra args...> — loops over SEEDS × TRACES
  local exp="$1"; shift
  local name="$1"; shift
  for trace in $TRACES; do
    for s in $SEEDS; do
      run_sim_one_seed "$trace" "$exp" "$name" "$s" "$@"
    done
  done
  echo "  $exp/$name  OK ($(echo $TRACES | wc -w) traces × $(echo $SEEDS | wc -w) seeds)"
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

# E5 — our v2 (E4 + M7 fragmentation requeue, with realistic ckpt cost)
step "E5 our-v2 (E4 + M7 fragmentation; ckpt_reload_cost=${CKPT_COST}s)"
run_sim e5 score-m7 --scheduler score --alpha 0.40 --beta 0.20 --delta 0.20 --epsilon 0.30 \
        --fragmentation --ckpt-reload-cost "$CKPT_COST"

# E5b — fragmentation with zero ckpt cost (matches pre-fix optimistic claim,
# so we can show how much of E5's gain came from ignoring the cost).
step "E5b fragmentation (ckpt_reload_cost=0 — optimistic upper bound)"
run_sim e5b score-m7-free --scheduler score --alpha 0.40 --beta 0.20 --delta 0.20 --epsilon 0.30 \
        --fragmentation --ckpt-reload-cost 0.0

# E6 — sensitivity grid. DENSE = 5×5 over (α,δ); SMALL = 3×3 legacy.
if [ "$E6_GRID" = "DENSE" ]; then
  ALPHAS="0.10 0.25 0.40 0.55 0.70"
  DELTAS="0.05 0.15 0.20 0.30 0.40"
else
  ALPHAS="0.20 0.40 0.60"
  DELTAS="0.10 0.20 0.30"
fi
step "E6 sensitivity grid ($E6_GRID)"
i=0
for a in $ALPHAS; do
  for d in $DELTAS; do
    name=$(printf "a%s_d%s" "$a" "$d")
    run_sim e6 "$name" --scheduler score --alpha "$a" --beta 0.20 --delta "$d" --epsilon 0.30
    i=$((i+1))
  done
done
echo "  E6: $i combos × $(echo $TRACES | wc -w) traces × $(echo $SEEDS | wc -w) seeds"

# Aggregate summaries for plotting
step "merging summaries"
"$PY" "$ROOT/eval/scripts/merge_summaries.py" "$OUT"

step "aggregating across seeds"
"$PY" "$ROOT/eval/scripts/aggregate_seeds.py" "$OUT"

step "done"
