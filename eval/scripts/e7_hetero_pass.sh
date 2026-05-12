#!/usr/bin/env bash
# Heterogeneous-workload single-pass driver. Same drain/sacct logic as
# e7_one_pass.sh but uses e7_jobs_hetero.sh (which spreads jobs across
# 5 simulated users via --comment so the M5 predictor's user-history
# features fire).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/eval/results/e7"
mkdir -p "$OUT"
KUBECTL="sudo kubectl"
NAMESPACE=${NAMESPACE:-slurm}
JOBS_SCRIPT="$ROOT/eval/scripts/e7_jobs_hetero.sh"
TAG="${1:?usage: e7_hetero_pass.sh <tag>}"
TIMEOUT_MIN=${TIMEOUT_MIN:-60}

submit() {
  echo "[e7h] === submitting $TAG workload (heterogeneous)"
  $KUBECTL -n "$NAMESPACE" exec deploy/slurm-login -- \
      bash -lc "$(cat "$JOBS_SCRIPT")" e7h_jobs "$TAG"
}

wait_drain() {
  echo "[e7h] waiting up to ${TIMEOUT_MIN}min for queue to drain"
  local deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
  local idle=0
  while (( $(date +%s) < deadline )); do
    local n
    n=$($KUBECTL -n "$NAMESPACE" exec deploy/slurm-login -- \
        bash -lc "squeue -h | wc -l" 2>/dev/null | tr -d '[:space:]' || echo "?")
    if [[ "$n" == "0" ]]; then
      idle=$((idle+1))
      [[ $idle -ge 2 ]] && { echo "[e7h]   drained"; return 0; }
    else
      idle=0
    fi
    printf '  %s jobs remaining\n' "$n"
    sleep 20
  done
  echo "[e7h] WARNING: timeout"
}

dump_sacct() {
  local csv="$OUT/${TAG}.csv"
  echo "[e7h] capturing sacct to $csv"
  $KUBECTL -n "$NAMESPACE" exec slurm-controller-0 -- bash -lc "
    sacct --noheader --parsable2 \
      --format=JobID,JobName,Submit,Start,End,Elapsed,State \
      --starttime=now-3hours
  " 2>/dev/null \
    | awk -F'|' -v t="${TAG}-" 'index($2,t)==1 && $1 !~ /\./' \
    > "$csv"
  wc -l < "$csv" | awk '{print "[e7h]   captured " $1 " rows"}'
}

echo "=== E7 heterogeneous pass — tag=$TAG ==="
submit
wait_drain
dump_sacct
echo "[e7h] pass $TAG done — $OUT/${TAG}.csv"
