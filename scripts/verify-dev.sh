#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
VERIFY_TIMEOUT_SECONDS=${VERIFY_TIMEOUT_SECONDS:-180}
BASELINE_WORKER_STS=${BASELINE_WORKER_STS:-slurm-worker-cpu}
BASELINE_WORKER_POD=${BASELINE_WORKER_POD:-slurm-worker-cpu-0}
LOGIN_SELECTOR=${LOGIN_SELECTOR:-app=slurm-login}
PARTITION=${PARTITION:-debug}

log() {
  echo "[dev verify] $*"
}

get_login_pod() {
  kubectl -n "$NAMESPACE" get pod -l "$LOGIN_SELECTOR" -o jsonpath='{.items[0].metadata.name}'
}

login_exec() {
  local pod
  pod=$(get_login_pod)
  kubectl -n "$NAMESPACE" exec "pod/${pod}" -- bash -lc "$1"
}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

log "checking core pods..."
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-controller-0 --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${BASELINE_WORKER_POD}" --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Available deployment/slurm-elastic-operator --timeout=120s
login_pod=$(get_login_pod)
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${login_pod}" --timeout=120s

log "phase1 functional checks..."
log "controller ping from login"
login_exec 'scontrol ping'

log "partition and node inventory"
login_exec 'sinfo'
login_exec "scontrol show node ${BASELINE_WORKER_POD}"

log "single-node srun smoke test"
srun_host=$(login_exec 'srun -N1 -n1 hostname' | tr -d '\r' | tail -n1)
echo "$srun_host"
if [[ "$srun_host" != "$BASELINE_WORKER_POD" ]]; then
  echo "unexpected srun host: ${srun_host} (expected ${BASELINE_WORKER_POD})" >&2
  exit 1
fi

log "single-node sbatch smoke test"
smoke_job_id=$(login_exec "cat <<'EOF_INNER' >/tmp/dev-smoke.sbatch
#!/bin/bash
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH -o /tmp/dev-smoke-%j.out
hostname
EOF_INNER
sbatch --parsable /tmp/dev-smoke.sbatch" | tr -d '\r' | tail -n1)
echo "submitted smoke job: ${smoke_job_id}"

start_ts=$(date +%s)
while true; do
  state=$(login_exec "squeue -h -j ${smoke_job_id} -o %T 2>/dev/null || true" | tr -d '\r' | tail -n1)
  if [[ -z "$state" ]]; then
    break
  fi
  now=$(date +%s)
  if (( now - start_ts > VERIFY_TIMEOUT_SECONDS )); then
    echo "smoke job ${smoke_job_id} did not finish in time" >&2
    login_exec "squeue -j ${smoke_job_id} || true"
    exit 1
  fi
  sleep 3
done
smoke_output=$(login_exec "cat /tmp/dev-smoke-${smoke_job_id}.out" | tr -d '\r' | tail -n1)
echo "$smoke_output"
if [[ "$smoke_output" != "$BASELINE_WORKER_POD" ]]; then
  echo "unexpected sbatch output host: ${smoke_output} (expected ${BASELINE_WORKER_POD})" >&2
  exit 1
fi

log "phase2 scale-up/scale-down checks..."
kubectl -n "$NAMESPACE" scale statefulset/${BASELINE_WORKER_STS} --replicas=1
kubectl -n "$NAMESPACE" rollout status statefulset/${BASELINE_WORKER_STS} --timeout=120s

job_id=$(login_exec "cat <<'EOF_INNER' >/tmp/phase2-job.sh
#!/bin/bash
#SBATCH -p ${PARTITION}
#SBATCH -N 2
sleep 60
EOF_INNER
sbatch --parsable /tmp/phase2-job.sh" | tr -d '\r' | tail -n1)
echo "submitted scale trigger job: ${job_id}"

start_ts=$(date +%s)
scaled_up=false
while true; do
  replicas=$(kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o jsonpath='{.spec.replicas}')
  if [[ "${replicas}" -ge 2 ]]; then
    scaled_up=true
    break
  fi
  now=$(date +%s)
  if (( now - start_ts > VERIFY_TIMEOUT_SECONDS )); then
    break
  fi
  sleep 5
done

if [[ "$scaled_up" != "true" ]]; then
  echo "operator did not scale up in time" >&2
  kubectl -n "$NAMESPACE" logs deployment/slurm-elastic-operator --tail=200 >&2 || true
  exit 1
fi

echo "scale-up verified: replicas=$(kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o jsonpath='{.spec.replicas}')"

login_exec "scancel ${job_id} || true"

sleep 90
replicas=$(kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o jsonpath='{.spec.replicas}')
if [[ "$replicas" -gt 1 ]]; then
  echo "warning: scale-down not completed yet (replicas=${replicas})" >&2
  kubectl -n "$NAMESPACE" logs deployment/slurm-elastic-operator --tail=200 >&2 || true
  exit 1
fi

echo "scale-down verified: replicas=${replicas}"
echo "[dev verify] done. phase1 + phase2 checks passed."
