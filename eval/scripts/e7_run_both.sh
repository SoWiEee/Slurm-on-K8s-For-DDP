#!/usr/bin/env bash
# E7 two-pass orchestrator: vendor (multifactor only) vs our (M3 score).
#
# Toggles `slurm.jobSubmit.enabled` via `helm upgrade --reuse-values`
# between passes. Each pass:
#   1. flip helm value & wait for controller to restart
#   2. submit the 20-job mix with the given pass tag
#   3. wait for queue to drain
#   4. capture sacct for the pass's job-name prefix
#
# Requires:
#   sudo cp /etc/rancher/k3s/k3s.yaml /tmp/k3s.yaml && sudo chmod 644 /tmp/k3s.yaml
#   (we use that as KUBECONFIG for helm; kubectl uses `sudo kubectl`)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/eval/results/e7"
mkdir -p "$OUT"

HELM=${HELM:-/home/acane/.local/bin/helm}
KUBECTL="sudo kubectl"
KUBECONFIG_FILE=${KUBECONFIG_FILE:-/tmp/k3s.yaml}
RELEASE=${RELEASE:-slurm-platform}
NAMESPACE=${NAMESPACE:-slurm}
JOBS_SCRIPT="$ROOT/eval/scripts/e7_jobs.sh"

if [[ ! -r "$KUBECONFIG_FILE" ]]; then
  echo "[e7] $KUBECONFIG_FILE unreadable — run:" >&2
  echo "       sudo cp /etc/rancher/k3s/k3s.yaml /tmp/k3s.yaml && sudo chmod 644 /tmp/k3s.yaml" >&2
  exit 1
fi
export KUBECONFIG="$KUBECONFIG_FILE"

helm_set() {
  # helm_set <key> <value>
  # Skip the upgrade entirely if the value is already correct — every
  # upgrade restarts slurm-controller, which transiently disconnects
  # workers and leaves running jobs in COMPLETING limbo. --no-hooks
  # avoids the one-shot gpu-labeler post-upgrade hook (already done).
  local current
  current=$("$HELM" get values "$RELEASE" -n "$NAMESPACE" -o json 2>/dev/null \
            | python3 -c "import json,sys; v=json.load(sys.stdin)
for k in '$1'.split('.'): v=v.get(k,{}) if isinstance(v,dict) else None
print(str(v).lower())" 2>/dev/null || echo "?")
  if [[ "$current" == "$2" ]]; then
    echo "[e7] $1 already = $2 — skipping helm upgrade"
    return 0
  fi
  echo "[e7] helm upgrade --set $1=$2 (--no-hooks)"
  "$HELM" upgrade "$RELEASE" "$ROOT/chart" -n "$NAMESPACE" \
       --reuse-values --set "$1=$2" --no-hooks --wait --timeout 3m >/dev/null
  echo "[e7]   controller restarted; will wait for workers to re-register"
}

wait_controller_ready() {
  echo "[e7] waiting for slurm-controller rollout"
  $KUBECTL -n "$NAMESPACE" rollout status sts/slurm-controller --timeout=180s >/dev/null
  # slurmctld is up but workers may take 10–30s to re-register and clear
  # their NOT_RESPONDING (*) suffix. Poll sinfo until at least one
  # gpu-rtx4070 node is idle without the * mark.
  echo "[e7] waiting for at least one gpu-rtx4070 node to be IDLE"
  for _ in $(seq 1 60); do
    if $KUBECTL -n "$NAMESPACE" exec slurm-controller-0 -- \
        sinfo -N -h -o "%N %T" 2>/dev/null \
        | grep -E "^slurm-worker-gpu-rtx4070-.*idle$" >/dev/null; then
      echo "[e7]   gpu pool ready"
      sleep 3
      return 0
    fi
    sleep 5
  done
  echo "[e7] WARNING: gpu pool never became IDLE — proceeding anyway"
}

submit_and_wait() {
  local tag="$1"
  echo "[e7] === pass: $tag === submitting workload"
  # Pass tag as $0/$1 via bash -lc positional args (avoids string-concat
  # bugs from `$(cat ...) $tag`, which used to glue "vendor" onto the last
  # line of the script).
  $KUBECTL -n "$NAMESPACE" exec deploy/slurm-login -- \
      bash -lc "$(cat "$JOBS_SCRIPT")" e7_jobs "$tag"
  echo "[e7] waiting for queue drain (no remaining jobs)"
  local idle=0
  while :; do
    local n
    n=$($KUBECTL -n "$NAMESPACE" exec deploy/slurm-login -- \
        bash -lc "squeue -h -o '%i' | wc -l" 2>/dev/null | tr -d '[:space:]' || echo "?")
    if [[ "$n" == "0" ]]; then
      idle=$((idle+1))
      # require 2 consecutive empty polls — guards against the brief gap
      # between a job's last step finishing and the next being scheduled.
      [[ $idle -ge 2 ]] && break
    else
      idle=0
    fi
    echo "  pending/running = $n"
    sleep 20
  done
  echo "[e7] queue drained"
}

dump_sacct() {
  local tag="$1"
  local csv="$OUT/${tag}.csv"
  echo "[e7] capturing sacct for ${tag}- → $csv"
  $KUBECTL -n "$NAMESPACE" exec deploy/slurm-login -- bash -lc "
    sacct --noheader --parsable2 \
      --format=JobID,JobName,Submit,Start,End,Elapsed,State \
      --starttime=now-3hours
  " 2>/dev/null \
    | awk -F'|' -v t="${tag}-" 'index($2,t)==1 && $1 !~ /\./' \
    > "$csv"
  wc -l < "$csv" | awk '{print "[e7]   captured " $1 " rows"}'
}

# ------------------- pass A: vendor (no lua) -------------------
helm_set slurm.jobSubmit.enabled false
wait_controller_ready
submit_and_wait vendor
dump_sacct vendor

# ------------------- pass B: our (M3 score) --------------------
helm_set slurm.jobSubmit.enabled true
wait_controller_ready
submit_and_wait our
dump_sacct our

# ------------------- compare -----------------------------------
"$ROOT/.venv-m5/bin/python" "$ROOT/eval/scripts/e7_compare.py" \
    "$OUT/vendor.csv" "$OUT/our.csv" | tee "$OUT/compare.txt"

echo "[e7] complete — see $OUT/{vendor.csv,our.csv,compare.txt}"
