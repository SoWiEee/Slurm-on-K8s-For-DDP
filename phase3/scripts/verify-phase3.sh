#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

kubectl get storageclass slurm-shared-nfs >/dev/null
kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx >/dev/null
kubectl -n "$NAMESPACE" wait --for=condition=Bound pvc/slurm-shared-rwx --timeout="$ROLLOUT_TIMEOUT"

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT"

for pod in slurm-controller-0 slurm-worker-0; do
  kubectl -n "$NAMESPACE" exec "pod/${pod}" -- test -d /shared
  kubectl -n "$NAMESPACE" exec "pod/${pod}" -- sh -c "mount | grep -q ' /shared '"
done

login_pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login -o jsonpath='{.items[0].metadata.name}')
kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- test -d /shared
kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- sh -c "mount | grep -q ' /shared '"

marker="phase3-$(date +%s)"
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sh -c "echo ${marker} > /shared/.phase3-marker"
kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- sh -c "grep -q ${marker} /shared/.phase3-marker"
kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- sh -c "grep -q ${marker} /shared/.phase3-marker"

echo "Phase 3 verification passed: shared RWX volume is mounted across controller/worker/login."
