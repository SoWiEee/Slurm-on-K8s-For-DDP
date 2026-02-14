#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KIND_CONFIG=${KIND_CONFIG:-}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
DOCKER_BUILD_NO_CACHE=${DOCKER_BUILD_NO_CACHE:-false}

if ! command -v kind >/dev/null 2>&1; then
  echo "kind is required" >&2
  exit 1
fi
if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  if [[ -n "$KIND_CONFIG" ]]; then
    kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG"
  else
    kind create cluster --name "$CLUSTER_NAME"
  fi
fi

build_flags=()
if [[ "$DOCKER_BUILD_NO_CACHE" == "true" ]]; then
  build_flags+=(--no-cache)
fi

docker build "${build_flags[@]}" -t slurm-controller:phase1 phase1/docker/controller
docker build "${build_flags[@]}" -t slurm-worker:phase1 phase1/docker/worker

kind load docker-image slurm-controller:phase1 --name "$CLUSTER_NAME"
kind load docker-image slurm-worker:phase1 --name "$CLUSTER_NAME"

phase1/scripts/create-secrets.sh slurm
kubectl apply -f phase1/manifests/slurm-static.yaml

set +e
kubectl -n slurm rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
rc1=$?
kubectl -n slurm rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"
rc2=$?
set -e

if [[ $rc1 -ne 0 || $rc2 -ne 0 ]]; then
  echo "[bootstrap] rollout failed, collecting diagnostics..." >&2
  kubectl -n slurm get pods -o wide || true
  kubectl -n slurm describe pods || true
  kubectl -n slurm logs statefulset/slurm-controller --all-containers=true --tail=200 || true
  kubectl -n slurm logs statefulset/slurm-controller --all-containers=true --previous --tail=200 || true
  kubectl -n slurm logs statefulset/slurm-worker --all-containers=true --tail=200 || true
  kubectl -n slurm logs statefulset/slurm-worker --all-containers=true --previous --tail=200 || true
  echo "[bootstrap] hint: if you see '/usr/bin/env: bash\\r', re-clone after .gitattributes or run with fresh checkout." >&2
  exit 1
fi

echo "Phase 1 deployment completed."
