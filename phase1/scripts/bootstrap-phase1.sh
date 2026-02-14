#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KIND_CONFIG=${KIND_CONFIG:-}

if ! command -v kind >/dev/null 2>&1; then
  echo "kind is required" >&2
  exit 1
fi

if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  if [[ -n "$KIND_CONFIG" ]]; then
    kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG"
  else
    kind create cluster --name "$CLUSTER_NAME"
  fi
fi

docker build -t slurm-controller:phase1 phase1/docker/controller
docker build -t slurm-worker:phase1 phase1/docker/worker

kind load docker-image slurm-controller:phase1 --name "$CLUSTER_NAME"
kind load docker-image slurm-worker:phase1 --name "$CLUSTER_NAME"

phase1/scripts/create-secrets.sh slurm
kubectl apply -f phase1/manifests/slurm-static.yaml

kubectl -n slurm rollout status statefulset/slurm-controller --timeout=180s
kubectl -n slurm rollout status statefulset/slurm-worker --timeout=180s

echo "Phase 1 deployment completed."
