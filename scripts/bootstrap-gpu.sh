#!/usr/bin/env bash
# bootstrap-gpu.sh — Deploy NVIDIA device plugin and optionally MPS DaemonSet
#
# Run AFTER bootstrap.sh on a Linux host with real GPUs.
#
# Usage:
#   bash scripts/bootstrap-gpu.sh             # device plugin only
#   bash scripts/bootstrap-gpu.sh --with-mps  # device plugin + MPS daemon

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
# Step 1: Deploy NVIDIA device plugin
# ---------------------------------------------------------------------------
log "deploying NVIDIA device plugin..."
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
else
  echo "$gpu_nodes" | while IFS=$'\t' read -r node gpu_count; do
    if [[ -n "$gpu_count" && "$gpu_count" != "0" ]]; then
      echo "  $node: nvidia.com/gpu=$gpu_count  OK"
    fi
  done
fi

# ---------------------------------------------------------------------------
# Step 3 (optional): Deploy MPS DaemonSet
# ---------------------------------------------------------------------------
if [[ "$WITH_MPS" == "true" ]]; then
  log "deploying MPS control daemon..."
  [[ -f manifests/gpu/mps-daemonset.yaml ]] || \
    die "manifests/gpu/mps-daemonset.yaml not found"

  kubectl apply -f manifests/gpu/mps-daemonset.yaml
  log "waiting for MPS DaemonSet..."
  kubectl -n "$NAMESPACE" rollout status daemonset/nvidia-mps-daemon --timeout=120s

  log "verifying MPS socket is available..."
  mps_pod=$(kubectl -n "$NAMESPACE" get pod -l app=nvidia-mps-daemon \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [[ -n "$mps_pod" ]]; then
    if kubectl -n "$NAMESPACE" exec "pod/${mps_pod}" -- \
        bash -c 'echo get_server_list | nvidia-cuda-mps-control' 2>/dev/null; then
      log "MPS control daemon responding OK"
    else
      echo "[bootstrap-gpu] WARNING: MPS control daemon not yet responding; check pod logs" >&2
    fi
  fi

  echo ""
  echo "[bootstrap-gpu] MPS deployed. Re-render manifests with MPS mounts:"
  echo "  REAL_GPU=true WITH_MPS=true K8S_RUNTIME=$K8S_RUNTIME bash scripts/bootstrap.sh"
fi

echo ""
log "GPU bootstrap done."
echo ""
echo "Next: run bootstrap.sh (or verify-gpu.sh to test GPU access from Slurm)"
echo "  REAL_GPU=true K8S_RUNTIME=$K8S_RUNTIME bash scripts/bootstrap.sh"
echo "  bash scripts/verify-gpu.sh"
