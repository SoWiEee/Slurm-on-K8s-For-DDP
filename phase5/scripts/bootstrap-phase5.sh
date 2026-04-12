#!/usr/bin/env bash
# bootstrap-phase5.sh — ensure Lmod module system is operational
#
# Lmod is now integrated into Phase 1:
#   - Images already include lmod (phase1/docker/controller + worker Dockerfiles)
#   - Modulefile ConfigMaps are in phase1/manifests/lmod-modulefiles.yaml
#   - render-slurm-static.py is called with --with-lmod in bootstrap-dev.sh and
#     bootstrap-phase1.sh, so volume mounts are always rendered
#
# This script only handles what is Phase 5-specific:
#   - Ensure Phase 1 and Phase 3 are deployed (NFS required for job output paths)
#   - Ensure /shared/jobs/ exists on the NFS volume
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
NAMESPACE=${NAMESPACE:-slurm}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found — run bootstrap-dev.sh first" >&2
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

# ---------------------------------------------------------------------------
# 1. Verify Phase 1 is deployed
# ---------------------------------------------------------------------------
if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller >/dev/null 2>&1; then
  echo "[phase5] Phase 1 not deployed — run: bash scripts/bootstrap-dev.sh" >&2
  exit 1
fi
echo "[phase5] Phase 1 deployed."

# ---------------------------------------------------------------------------
# 2. Verify Phase 3 NFS is deployed (required for /shared/jobs output path)
# ---------------------------------------------------------------------------
if ! kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx >/dev/null 2>&1; then
  echo "[phase5] Phase 3 NFS PVC not found — run: NFS_SERVER=<ip> bash phase3/scripts/bootstrap-phase3.sh" >&2
  exit 1
fi
echo "[phase5] Phase 3 NFS PVC detected."

# ---------------------------------------------------------------------------
# 3. Apply lmod modulefile ConfigMaps (idempotent; already done by Phase 1
#    bootstrap, but safe to re-apply here in case of manual cluster teardown)
# ---------------------------------------------------------------------------
echo "[phase5] applying lmod modulefile ConfigMaps..."
kubectl apply -f phase1/manifests/lmod-modulefiles.yaml

# ---------------------------------------------------------------------------
# 4. Ensure /shared/jobs/ exists on NFS
# ---------------------------------------------------------------------------
echo "[phase5] ensuring /shared/jobs directory exists..."
kubectl -n "$NAMESPACE" wait pod/slurm-controller-0 --for=condition=Ready --timeout=120s >/dev/null
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- mkdir -p /shared/jobs || true

echo ""
echo "Phase 5 ready.  Run: bash phase5/scripts/verify-phase5.sh"
