#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
NAMESPACE=${NAMESPACE:-slurm}
VERIFY_TIMEOUT_SECONDS=${VERIFY_TIMEOUT_SECONDS:-180}

if ! command -v bash >/dev/null 2>&1; then
  echo "bash is required" >&2
  exit 1
fi

echo "[dev verify] Phase 1 checks..."
CLUSTER_NAME="$CLUSTER_NAME" \
KUBE_CONTEXT="$KUBE_CONTEXT" \
NAMESPACE="$NAMESPACE" \
bash phase1/scripts/verify-phase1.sh

echo "[dev verify] Phase 2 scaling checks..."
CLUSTER_NAME="$CLUSTER_NAME" \
KUBE_CONTEXT="$KUBE_CONTEXT" \
NAMESPACE="$NAMESPACE" \
VERIFY_TIMEOUT_SECONDS="$VERIFY_TIMEOUT_SECONDS" \
bash phase2/scripts/verify-phase2.sh

echo "[dev verify] Done. Phase 1 + Phase 2 verification passed."
