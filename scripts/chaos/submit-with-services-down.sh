#!/usr/bin/env bash
# Measure submit-path fallback behaviour while optional scheduler services are down.
#
# This script intentionally scales optional Deployments to zero, submits short
# Slurm jobs through the login pod, records sbatch latency, then restores the
# original replica counts. It is meant for a live k3s/dev cluster, not CI.
#
# Example:
#   KUBECONFIG=~/.kube/config SAMPLES=20 bash scripts/chaos/submit-with-services-down.sh

set -euo pipefail

NAMESPACE="${NAMESPACE:-slurm}"
SAMPLES="${SAMPLES:-10}"
PARTITION="${PARTITION:-cpu}"
JOB_WRAP="${JOB_WRAP:-true}"
JOB_PREFIX="${JOB_PREFIX:-chaos-submit}"
OUT_DIR="${OUT_DIR:-/tmp/kelpflux-submit-chaos-$(date +%Y%m%d-%H%M%S)}"
SERVICES=(rl-scheduler runtime-predictor weight-tuner)

log() { printf '[submit-chaos] %s\n' "$*"; }
warn() { printf '[submit-chaos][WARN] %s\n' "$*" >&2; }
fail() { printf '[submit-chaos][ERROR] %s\n' "$*" >&2; exit 1; }

command -v kubectl >/dev/null || fail "kubectl not found"
command -v python3 >/dev/null || fail "python3 not found"
[[ "$SAMPLES" =~ ^[0-9]+$ ]] && [[ "$SAMPLES" -gt 0 ]] || fail "SAMPLES must be a positive integer"

mkdir -p "$OUT_DIR"
LATENCY_TSV="$OUT_DIR/latency.tsv"
printf 'phase\titeration\tlatency_ms\tstatus\tjob_id\n' > "$LATENCY_TSV"

declare -A ORIGINAL_REPLICAS=()

deployment_exists() {
  kubectl -n "$NAMESPACE" get deploy "$1" >/dev/null 2>&1
}

replicas_of() {
  kubectl -n "$NAMESPACE" get deploy "$1" -o jsonpath='{.spec.replicas}' 2>/dev/null || true
}

scale_deploy() {
  local name="$1" replicas="$2"
  if deployment_exists "$name"; then
    log "scale deploy/$name -> $replicas"
    kubectl -n "$NAMESPACE" scale deploy "$name" --replicas="$replicas" >/dev/null
    if [[ "$replicas" -gt 0 ]]; then
      kubectl -n "$NAMESPACE" rollout status deploy "$name" --timeout=180s >/dev/null
    fi
  else
    warn "deploy/$name does not exist; skipping"
  fi
}

restore() {
  local name replicas
  log "restoring deployments"
  for name in "${!ORIGINAL_REPLICAS[@]}"; do
    replicas="${ORIGINAL_REPLICAS[$name]}"
    scale_deploy "$name" "$replicas" || warn "failed to restore deploy/$name"
  done
  log "results: $LATENCY_TSV"
}
trap restore EXIT

for svc in "${SERVICES[@]}"; do
  if deployment_exists "$svc"; then
    ORIGINAL_REPLICAS[$svc]="$(replicas_of "$svc")"
  fi
done

LOGIN_POD="$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login -o jsonpath='{.items[0].metadata.name}')"
[[ -n "$LOGIN_POD" ]] || fail "no slurm-login pod found"
log "login pod: $LOGIN_POD"

submit_phase() {
  local phase="$1" i start_ns end_ns latency_ms output status job_name job_id
  log "phase=$phase samples=$SAMPLES"
  for i in $(seq 1 "$SAMPLES"); do
    job_name="${JOB_PREFIX}-${phase}-${i}"
    start_ns="$(date +%s%N)"
    set +e
    output="$(kubectl -n "$NAMESPACE" exec "$LOGIN_POD" -- \
      sbatch --parsable --job-name="$job_name" -p "$PARTITION" --wrap="$JOB_WRAP" 2>&1)"
    status="$?"
    set -e
    end_ns="$(date +%s%N)"
    latency_ms=$(( (end_ns - start_ns) / 1000000 ))
    if [[ "$status" -eq 0 ]]; then
      job_id="$(printf '%s' "$output" | head -n1 | cut -d';' -f1)"
      printf '%s\t%s\t%s\tok\t%s\n' "$phase" "$i" "$latency_ms" "$job_id" >> "$LATENCY_TSV"
    else
      warn "phase=$phase iteration=$i failed: $output"
      printf '%s\t%s\t%s\tfail\t%s\n' "$phase" "$i" "$latency_ms" "$(printf '%s' "$output" | tr '\t\n' '  ')" >> "$LATENCY_TSV"
    fi
  done
}

submit_phase baseline

for svc in "${SERVICES[@]}"; do
  scale_deploy "$svc" 0
  submit_phase "${svc}-down"
  if [[ -n "${ORIGINAL_REPLICAS[$svc]:-}" ]]; then
    scale_deploy "$svc" "${ORIGINAL_REPLICAS[$svc]}"
  fi
done

for svc in "${SERVICES[@]}"; do
  scale_deploy "$svc" 0
done
submit_phase all-optional-services-down

python3 - "$LATENCY_TSV" <<'PYSTATS'
import csv
import statistics
import sys
from collections import defaultdict

path = sys.argv[1]
rows = defaultdict(list)
fails = defaultdict(int)
with open(path, newline="") as fh:
    reader = csv.DictReader(fh, delimiter="\t")
    for row in reader:
        phase = row["phase"]
        if row["status"] == "ok":
            rows[phase].append(float(row["latency_ms"]))
        else:
            fails[phase] += 1

print("\nphase\tn\tfail\tp50_ms\tp95_ms\tp99_ms\tmax_ms")
for phase in sorted(set(rows) | set(fails)):
    vals = sorted(rows.get(phase, []))
    if not vals:
        print(f"{phase}\t0\t{fails[phase]}\tNA\tNA\tNA\tNA")
        continue
    def pct(p):
        idx = min(len(vals) - 1, max(0, round((p / 100) * (len(vals) - 1))))
        return vals[idx]
    print(
        f"{phase}\t{len(vals)}\t{fails[phase]}\t"
        f"{statistics.median(vals):.0f}\t{pct(95):.0f}\t{pct(99):.0f}\t{max(vals):.0f}"
    )
PYSTATS

log "raw samples written to $LATENCY_TSV"
