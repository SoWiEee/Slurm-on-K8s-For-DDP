#!/usr/bin/env bash
# eval/scripts/run_e7_live.sh — E7 live-cluster evaluation.
#
# Submits a 50-job mix to the live Slurm-on-K8s cluster (kubectl context
# must already point at it; gpu-rtx4070 partition must exist) and records
# per-job submit/start/end timestamps from sacct after the run drains.
#
# Two passes: (a) operator running the M3+M5+M7 stack, (b) operator
# replaced by a stock multifactor-only baseline. The harness assumes you
# `helm upgrade` between passes — the script doesn't try to rewrite chart
# values, just submits and records.
#
# Output: eval/results/e7/<pass>.csv (job_id, submit, start, end, runtime)
#         eval/results/e7/<pass>.json — wall-clock JCT mean / p90 / p95
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/eval/results/e7"
mkdir -p "$OUT"

PASS="${1:-our}"   # "our" (M3+M5+M7) or "vendor" (multifactor-only baseline)
N_JOBS="${N_JOBS:-50}"
MIX_SCRIPT="$ROOT/eval/scripts/e7_jobs.sh"

if [[ ! -x "$MIX_SCRIPT" ]]; then
  echo "missing $MIX_SCRIPT — generate the 50-job mix first" >&2
  exit 1
fi

echo "[e7] pass=$PASS n_jobs=$N_JOBS — submitting via login pod"
kubectl -n slurm exec deploy/slurm-login -- bash -lc "$(cat "$MIX_SCRIPT")"

echo "[e7] waiting for queue to drain"
until [[ -z "$(kubectl -n slurm exec deploy/slurm-login -- bash -lc 'squeue -h -o %i' 2>/dev/null)" ]]; do
  sleep 30
done

echo "[e7] collecting sacct"
csv="$OUT/$PASS.csv"
kubectl -n slurm exec deploy/slurm-login -- bash -lc "
  sacct --noheader --parsable2 \
    --format=JobID,JobName,Submit,Start,End,Elapsed,State \
    --starttime=now-2hours
" > "$csv.raw"

# Filter to non-batch step rows + completed/failed jobs only.
awk -F'|' 'NR>0 && $1 !~ /\./ && ($7=="COMPLETED" || $7=="FAILED" || $7=="REQUEUED") {print}' \
  "$csv.raw" > "$csv"
rm -f "$csv.raw"

# Compute summary
"$ROOT/.venv-m5/bin/python" "$ROOT/eval/scripts/e7_summarise.py" \
  "$csv" > "$OUT/$PASS.json"

echo "[e7] done — see $OUT/$PASS.{csv,json}"
