#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-slurm-lab}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-180s}
ENFORCE_RUNTIME=${ENFORCE_RUNTIME:-true}
PHASE2E_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

log() {
  echo "[phase2-e] $*"
}

die() {
  echo "[phase2-e][ERROR] $*" >&2
  exit 1
}

require() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

patch_network_annotation() {
  local kindname="$1"
  local name="$2"
  local networks_json="$3"
  local escaped
  local patch_json

  kubectl -n "$NAMESPACE" get "$kindname/$name" >/dev/null 2>&1 || return 0

  escaped=$(printf '%s' "$networks_json")
  escaped=${escaped//$'\n'/}
  escaped=${escaped//$'\r'/}
  escaped=${escaped//\\/\\\\}
  escaped=${escaped//\"/\\\"}
  patch_json=$(printf '{"spec":{"template":{"metadata":{"annotations":{"k8s.v1.cni.cncf.io/networks":"%s"}}}}}' "$escaped")

  kubectl -n "$NAMESPACE" patch "$kindname/$name" --type merge -p "$patch_json" >/dev/null
}

remove_network_annotation() {
  local kindname="$1"
  local name="$2"

  kubectl -n "$NAMESPACE" get "$kindname/$name" >/dev/null 2>&1 || return 0
  kubectl -n "$NAMESPACE" annotate "$kindname/$name" k8s.v1.cni.cncf.io/networks- >/dev/null 2>&1 || true
}

require kubectl
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

if ! kubectl api-resources 2>/dev/null | grep -q '^network-attachment-definitions'; then
  if [[ "$ENFORCE_RUNTIME" == "true" ]]; then
    die "Multus CRD not detected. Install Multus first or run ENFORCE_RUNTIME=false for topology-only patching."
  fi
  log "Multus CRD not detected; continuing in topology-only mode"
fi

log "applying Phase 2-E topology config"
kubectl apply -f "$PHASE2E_DIR/manifests/slurm-phaseE-topology.yaml"

if kubectl api-resources 2>/dev/null | grep -q '^network-attachment-definitions'; then
  log "applying Phase 2-E runtime assets"
  kubectl apply -f "$PHASE2E_DIR/manifests/slurm-phaseE-runtime.yaml"
fi

DATA_ONLY='[{"name":"slurm-data-net","interface":"net2"}]'

log "patching pod template annotations"
remove_network_annotation statefulset slurm-controller
remove_network_annotation deployment slurm-elastic-operator
patch_network_annotation deployment slurm-login "$DATA_ONLY"
patch_network_annotation statefulset slurm-worker-cpu "$DATA_ONLY"
patch_network_annotation statefulset slurm-worker-gpu-a10 "$DATA_ONLY"
patch_network_annotation statefulset slurm-worker-gpu-h100 "$DATA_ONLY"
# controller/operator stay on the default primary pod network and receive no secondary attachment.

for res in deployment/slurm-login statefulset/slurm-worker-cpu statefulset/slurm-worker-gpu-a10 statefulset/slurm-worker-gpu-h100 statefulset/slurm-controller deployment/slurm-elastic-operator; do
  kubectl -n "$NAMESPACE" get "$res" >/dev/null 2>&1 || continue
  kubectl -n "$NAMESPACE" rollout restart "$res" >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" rollout status "$res" --timeout="$ROLLOUT_TIMEOUT" >/dev/null 2>&1 || true
done

log "Phase 2-E MVP applied"
log "next step: run phase2/scripts/verify-network.sh"
