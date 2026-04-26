#!/usr/bin/env bash
# bootstrap-gpu.sh — Deploy NVIDIA device plugin (with built-in MPS sharing)
#
# Run AFTER bootstrap.sh on a Linux host with real GPUs.
#
# Usage:
#   bash scripts/bootstrap-gpu.sh             # device plugin + MPS sharing config
#   bash scripts/bootstrap-gpu.sh --with-mps  # alias of the above (kept for
#                                             # back-compat with older docs)
#
# Note: --with-mps is now a no-op flag. MPS is always provided by the
# device-plugin's `sharing.mps` config in manifests/gpu/nvidia-device-plugin.yaml.
# The previous self-hosted MPS DaemonSet has been deprecated (see
# manifests/gpu/mps-daemonset.yaml for context).

set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
K8S_RUNTIME=${K8S_RUNTIME:-kind}
KUBE_CONTEXT=${KUBE_CONTEXT:-$([[ "$K8S_RUNTIME" == "k3s" ]] && echo "default" || echo "kind-${CLUSTER_NAME}")}
WITH_MPS=${WITH_MPS:-false}
for arg in "$@"; do
  [[ "$arg" == "--with-mps" ]] && WITH_MPS=true
done

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

log() { echo "[bootstrap-gpu] $*"; }
die() { echo "[bootstrap-gpu][ERROR] $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Verify NVIDIA device plugin manifest exists
# ---------------------------------------------------------------------------
[[ -f manifests/gpu/nvidia-device-plugin.yaml ]] || \
  die "manifests/gpu/nvidia-device-plugin.yaml not found"

# ---------------------------------------------------------------------------
# Step 1: Deploy NVIDIA device plugin (with built-in MPS sharing)
# ---------------------------------------------------------------------------
log "deploying NVIDIA device plugin (sharing.mps enabled)..."
kubectl apply -f manifests/gpu/nvidia-device-plugin.yaml

log "waiting for device plugin DaemonSet to be ready..."
kubectl -n kube-system rollout status daemonset/nvidia-device-plugin-daemonset --timeout=120s

# ---------------------------------------------------------------------------
# Step 2: Verify GPU resources are advertised
# ---------------------------------------------------------------------------
log "checking GPU resource availability on nodes..."
sleep 5
gpu_nodes=$(kubectl get nodes \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' \
  2>/dev/null | grep -v '^$' || true)

if [[ -z "$gpu_nodes" ]]; then
  echo "[bootstrap-gpu] WARNING: no nodes advertising nvidia.com/gpu yet" >&2
  echo "  This may take a moment. Check: kubectl describe nodes | grep -A5 Capacity" >&2
  echo "  Also confirm the node carries the label" >&2
  echo "    nvidia.com/device-plugin.config=rtx5070-mps  (for RTX 5070 hosts)" >&2
  echo "    nvidia.com/device-plugin.config=rtx4080-exclusive  (for RTX 4080 hosts)" >&2
else
  echo "$gpu_nodes" | while IFS=$'\t' read -r node gpu_count; do
    if [[ -n "$gpu_count" && "$gpu_count" != "0" ]]; then
      echo "  $node: nvidia.com/gpu=$gpu_count  OK"
    fi
  done
fi

# ---------------------------------------------------------------------------
# Step 3: --with-mps flag (back-compat no-op)
# ---------------------------------------------------------------------------
if [[ "$WITH_MPS" == "true" ]]; then
  log "--with-mps is a no-op — MPS is provided by device-plugin sharing.mps"
  log "(self-hosted MPS DaemonSet was deprecated; see manifests/gpu/mps-daemonset.yaml)"
fi

echo ""
log "GPU bootstrap done."
echo ""
echo "Next: run bootstrap.sh (or verify-gpu.sh to test GPU access from Slurm)"
echo "  REAL_GPU=true K8S_RUNTIME=$K8S_RUNTIME bash scripts/bootstrap.sh"
echo "  bash scripts/verify-gpu.sh"
