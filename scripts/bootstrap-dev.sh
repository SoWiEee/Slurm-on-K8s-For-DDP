#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
NAMESPACE=${NAMESPACE:-slurm}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
DOCKER_BUILD_NO_CACHE=${DOCKER_BUILD_NO_CACHE:-false}
FORCE_RECREATE=${FORCE_RECREATE:-false}
KIND_CONFIG=${KIND_CONFIG:-}

if ! command -v bash >/dev/null 2>&1; then
  echo "bash is required" >&2
  exit 1
fi

echo "[dev bootstrap] Phase 1: deploying static Slurm cluster..."
CLUSTER_NAME="$CLUSTER_NAME" \
KUBE_CONTEXT="$KUBE_CONTEXT" \
NAMESPACE="$NAMESPACE" \
ROLLOUT_TIMEOUT="$ROLLOUT_TIMEOUT" \
DOCKER_BUILD_NO_CACHE="$DOCKER_BUILD_NO_CACHE" \
FORCE_RECREATE="$FORCE_RECREATE" \
KIND_CONFIG="$KIND_CONFIG" \
bash phase1/scripts/bootstrap-phase1.sh

echo "[dev bootstrap] Phase 2: deploying elastic operator..."
CLUSTER_NAME="$CLUSTER_NAME" \
KUBE_CONTEXT="$KUBE_CONTEXT" \
NAMESPACE="$NAMESPACE" \
ROLLOUT_TIMEOUT="$ROLLOUT_TIMEOUT" \
DOCKER_BUILD_NO_CACHE="$DOCKER_BUILD_NO_CACHE" \
bash phase2/scripts/bootstrap-phase2.sh

echo "[dev bootstrap] Done. Phase 1 + Phase 2 are ready on context: ${KUBE_CONTEXT}"
