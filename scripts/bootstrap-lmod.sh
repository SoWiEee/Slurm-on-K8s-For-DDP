#!/usr/bin/env bash
# bootstrap-lmod.sh — ensure Lmod module system is operational
#
# Lmod is integrated into the core cluster (docker/controller + docker/worker),
# so this script only handles what is Lmod-specific at runtime:
#   - Verify the cluster (bootstrap.sh) and NFS storage (bootstrap-storage.sh) are deployed
#   - Apply modulefile ConfigMaps (manifests/core/lmod-modulefiles.yaml) — idempotent
#   - Ensure /shared/jobs/ exists on the NFS volume (required for job output paths)
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
NAMESPACE=${NAMESPACE:-slurm}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found — run bootstrap.sh first" >&2
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

# ---------------------------------------------------------------------------
# 1. Verify Phase 1 is deployed
# ---------------------------------------------------------------------------
if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller >/dev/null 2>&1; then
  echo "[bootstrap-lmod] Cluster not deployed — run: bash scripts/bootstrap.sh" >&2
  exit 1
fi
echo "[bootstrap-lmod] Phase 1 deployed."

# ---------------------------------------------------------------------------
# 2. Verify Phase 3 NFS is deployed (required for /shared/jobs output path)
# ---------------------------------------------------------------------------
if ! kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx >/dev/null 2>&1; then
  echo "[bootstrap-lmod] NFS PVC not found — run: NFS_SERVER=<ip> bash scripts/bootstrap-storage.sh" >&2
  exit 1
fi
echo "[bootstrap-lmod] Phase 3 NFS PVC detected."

# ---------------------------------------------------------------------------
# 3. Apply lmod modulefile ConfigMaps (idempotent; already done by Phase 1
#    bootstrap, but safe to re-apply here in case of manual cluster teardown)
# ---------------------------------------------------------------------------
echo "[bootstrap-lmod] applying lmod modulefile ConfigMaps..."
kubectl apply -f manifests/core/lmod-modulefiles.yaml

# ---------------------------------------------------------------------------
# 4. Ensure /shared/jobs/ exists on NFS
# ---------------------------------------------------------------------------
echo "[bootstrap-lmod] ensuring /shared/jobs directory exists..."
kubectl -n "$NAMESPACE" wait pod/slurm-controller-0 --for=condition=Ready --timeout=120s >/dev/null
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- mkdir -p /shared/jobs || true

echo ""
echo "Lmod ready.  Run: bash scripts/verify-lmod.sh"
