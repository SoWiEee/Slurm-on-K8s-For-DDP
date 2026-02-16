#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
VERIFY_TIMEOUT_SECONDS=${VERIFY_TIMEOUT_SECONDS:-180}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

echo "[dev verify] checking core pods..."
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-controller-0 --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-0 --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Available deployment/slurm-elastic-operator --timeout=120s

echo "[dev verify] phase1 functional checks..."
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sinfo
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- scontrol show nodes
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc 'ssh -o StrictHostKeyChecking=no slurm-worker-0.slurm-worker hostname'

echo "[dev verify] phase2 scale-up/scale-down checks..."
# Ensure start point is 1 replica so pending jobs can trigger scale-up.
kubectl -n "$NAMESPACE" scale statefulset/slurm-worker --replicas=1
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout=120s

job_id=$(kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "cat <<'EOF_INNER' >/tmp/phase2-job.sh
#!/bin/bash
sleep 120
EOF_INNER
sbatch --parsable -N 2 /tmp/phase2-job.sh")
echo "submitted job: ${job_id}"

start_ts=$(date +%s)
scaled_up=false
while true; do
  replicas=$(kubectl -n "$NAMESPACE" get statefulset/slurm-worker -o jsonpath='{.spec.replicas}')
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

echo "scale-up verified: replicas=$(kubectl -n "$NAMESPACE" get statefulset/slurm-worker -o jsonpath='{.spec.replicas}')"

kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- scancel "$job_id"

# Wait for cooldown + polling and verify scale-down.
sleep 90
replicas=$(kubectl -n "$NAMESPACE" get statefulset/slurm-worker -o jsonpath='{.spec.replicas}')
if [[ "$replicas" -gt 1 ]]; then
  echo "warning: scale-down not completed yet (replicas=${replicas})" >&2
  kubectl -n "$NAMESPACE" logs deployment/slurm-elastic-operator --tail=200 >&2 || true
  exit 1
fi

echo "scale-down verified: replicas=${replicas}"
echo "[dev verify] done. phase1 + phase2 checks passed."
