#!/usr/bin/env bash
# Single-pass E7 runner. Assumes the cluster is already configured for
# the desired pass (vendor vs our). Submits the 20-job mix tagged with
# the pass name, waits for queue drain, then dumps sacct.
#
# Designed to be hands-off but resilient: no helm operations, no
# scheduler config changes. Use this twice — once with jobSubmit=false,
# once with jobSubmit=true — toggling the chart values between runs.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/eval/results/e7"
mkdir -p "$OUT"

KUBECTL="sudo kubectl"
NAMESPACE=${NAMESPACE:-slurm}
JOBS_SCRIPT="$ROOT/eval/scripts/e7_jobs.sh"
TAG="${1:?usage: e7_one_pass.sh <tag>  (e.g. vendor / our)}"
TIMEOUT_MIN=${TIMEOUT_MIN:-60}

wait_gpu_idle_or_off() {
  echo "[e7] checking gpu pool state"
  for _ in $(seq 1 30); do
    local state
    state=$($KUBECTL -n "$NAMESPACE" exec slurm-controller-0 -- \
            sinfo -N -h -o '%N %T' 2>/dev/null \
            | awk '/^slurm-worker-gpu-rtx4070-/' || true)
    # OK if all gpu nodes are idle (no '*') OR the StatefulSet is at 0
    if [[ -z "$state" ]]; then
      echo "[e7]   no gpu workers — operator will scale up after submit"
      return 0
    fi
    if ! echo "$state" | grep -qE '\*|down|drain'; then
      echo "[e7]   gpu pool fully idle"
      return 0
    fi
    echo "[e7]   gpu state: $(echo "$state" | tr '\n' ';')"
    sleep 6
  done
  echo "[e7] WARNING: gpu pool not fully idle — proceeding anyway"
}

submit() {
  echo "[e7] === submitting $TAG workload"
  $KUBECTL -n "$NAMESPACE" exec deploy/slurm-login -- \
      bash -lc "$(cat "$JOBS_SCRIPT")" e7_jobs "$TAG"
}

wait_drain() {
  echo "[e7] waiting up to ${TIMEOUT_MIN}min for queue to drain"
  local deadline=$(( $(date +%s) + TIMEOUT_MIN * 60 ))
  local idle=0
  while (( $(date +%s) < deadline )); do
    local n
    n=$($KUBECTL -n "$NAMESPACE" exec deploy/slurm-login -- \
        bash -lc "squeue -h -n $(awk -v t="$TAG-" '
          BEGIN { for (i=0; i<12; i++) printf t i "-s,"; for (i=12; i<18; i++) printf t i "-m,"; for (i=18; i<20; i++) printf t i "-l,"; print "x" }')" 2>/dev/null \
        | wc -l | tr -d '[:space:]' || echo "?")
    if [[ "$n" == "0" ]]; then
      idle=$((idle+1))
      [[ $idle -ge 2 ]] && { echo "[e7]   drained"; return 0; }
    else
      idle=0
    fi
    printf '  t=%ds  %s jobs remaining\n' \
      "$(( $(date +%s) - (deadline - TIMEOUT_MIN*60) ))" "$n"
    sleep 20
  done
  echo "[e7] WARNING: timeout"
}

dump_sacct() {
  local csv="$OUT/${TAG}.csv"
  echo "[e7] capturing sacct to $csv"
  # Use controller pod directly — slurmdbd has been seen to refuse
  # connections from the login pod after controller restarts. The
  # controller's sacct talks to slurmdbd over the same internal socket
  # that slurmctld uses, so this path is more reliable.
  $KUBECTL -n "$NAMESPACE" exec slurm-controller-0 -- bash -lc "
    sacct --noheader --parsable2 \
      --format=JobID,JobName,Submit,Start,End,Elapsed,State \
      --starttime=now-3hours
  " 2>/dev/null \
    | awk -F'|' -v t="${TAG}-" 'index($2,t)==1 && $1 !~ /\./' \
    > "$csv"
  wc -l < "$csv" | awk '{print "[e7]   captured " $1 " rows"}'
}

echo "=== E7 single-pass — tag=$TAG ==="
wait_gpu_idle_or_off
submit
wait_drain
dump_sacct
echo "[e7] pass $TAG done — $OUT/${TAG}.csv"
