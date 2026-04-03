#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
DOCKER_BUILD_NO_CACHE=${DOCKER_BUILD_NO_CACHE:-false}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller slurm-worker-cpu >/dev/null 2>&1; then
  echo "Phase 1 resources not found in namespace ${NAMESPACE}. run phase1/scripts/bootstrap-phase1.sh first." >&2
  exit 1
fi

build_flags=()
if [[ "$DOCKER_BUILD_NO_CACHE" == "true" ]]; then
  build_flags+=(--no-cache)
fi

docker build "${build_flags[@]}" -t slurm-elastic-operator:phase2 -f phase2/docker/operator/Dockerfile .
kind load docker-image slurm-elastic-operator:phase2 --name "$CLUSTER_NAME"

kubectl apply -f phase2/manifests/slurm-phase2-operator.yaml
kubectl apply -f phase2/manifests/network-policy.yaml

# Do NOT apply a partial StatefulSet manifest (will fail validation/update constraints).
# Scale existing Phase 1 worker StatefulSet in-place so operator can take over from 1 replica.
kubectl -n "$NAMESPACE" scale statefulset/slurm-worker-cpu --replicas=1

kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker-cpu --timeout="$ROLLOUT_TIMEOUT"

echo "Phase 2 deployment completed."
