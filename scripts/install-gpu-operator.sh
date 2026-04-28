#!/usr/bin/env bash
# Install NVIDIA GPU Operator into its own namespace.
#
# Why this isn't a slurm-platform chart dependency: GPU Operator hardcodes
# Release.Namespace for all DaemonSets and needs that namespace to be
# PSS=privileged for hostPath access (driver libs, /dev/nvidia*,
# /run/nvidia/mps). Our slurm namespace is PSS=baseline so the two cannot
# cohabit. See docs/note.md §5-A and docs/migration.md.
#
# Prerequisites:
#   - host has nvidia-driver-535 + nvidia-container-toolkit installed
#     (scripts/setup-linux-gpu.sh handles that)
#   - slurm-platform chart already installed OR will be installed; the
#     chart contributes the device-plugin-config ConfigMap that this
#     script references via --set devicePlugin.config.name=...
#
# Idempotent: re-running this script upgrades the existing release.

set -euo pipefail

NAMESPACE="${NAMESPACE:-gpu-operator}"
RELEASE="${RELEASE:-gpu-operator}"
VERSION="${VERSION:-v26.3.1}"
DEVICE_PLUGIN_CONFIG="${DEVICE_PLUGIN_CONFIG:-slurm-platform-device-plugin-config}"
DEFAULT_CONFIG_KEY="${DEFAULT_CONFIG_KEY:-default}"
MPS_ROOT="${MPS_ROOT:-/run/nvidia/mps}"
HELM_TIMEOUT="${HELM_TIMEOUT:-10m}"
DRY_RUN="${DRY_RUN:-}"

log()  { echo "[install-gpu-operator] $*"; }
warn() { echo "[install-gpu-operator][WARN] $*" >&2; }
fail() { echo "[install-gpu-operator][ERROR] $*" >&2; exit 1; }

command -v helm >/dev/null    || fail "helm not found in PATH"
command -v kubectl >/dev/null || fail "kubectl not found in PATH"

# Ensure NGC repo is available (idempotent).
if ! helm repo list 2>/dev/null | grep -q '^nvidia\b'; then
  log "adding nvidia helm repo (NGC)"
  helm repo add nvidia https://helm.ngc.nvidia.com/nvidia >/dev/null
fi
log "refreshing nvidia helm repo"
helm repo update nvidia >/dev/null

# slurm-platform chart pre-creates the gpu-operator namespace via its
# pre-install hook; this is a fallback for the scenario where users want to
# install gpu-operator before slurm-platform.
if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  log "creating namespace $NAMESPACE (PSS=privileged)"
  kubectl create namespace "$NAMESPACE"
  kubectl label ns "$NAMESPACE" \
    pod-security.kubernetes.io/enforce=privileged \
    pod-security.kubernetes.io/audit=privileged \
    pod-security.kubernetes.io/warn=privileged \
    --overwrite >/dev/null
fi

# Warn if the device-plugin-config ConfigMap is missing — gpu-operator's
# device-plugin will fail its init container without it.
if ! kubectl -n "$NAMESPACE" get configmap "$DEVICE_PLUGIN_CONFIG" >/dev/null 2>&1; then
  warn "ConfigMap $NAMESPACE/$DEVICE_PLUGIN_CONFIG not found yet"
  warn "  → install slurm-platform chart first, or this release will roll once the ConfigMap appears"
fi

action="install"
if helm -n "$NAMESPACE" status "$RELEASE" >/dev/null 2>&1; then
  action="upgrade"
fi

log "${action}: gpu-operator $VERSION → $NAMESPACE/$RELEASE"
helm_args=(
  "$action" "$RELEASE" nvidia/gpu-operator
  -n "$NAMESPACE"
  --version "$VERSION"
  --timeout "$HELM_TIMEOUT"
  --wait
  # Host already has nvidia-driver-535 + nvidia-container-toolkit installed
  # by scripts/setup-linux-gpu.sh; double-installation conflicts.
  --set driver.enabled=false
  --set toolkit.enabled=false
  # Point the device-plugin at our chart's ConfigMap so sharing.mps /
  # time-slicing strategies are picked up per node label.
  --set devicePlugin.config.name="$DEVICE_PLUGIN_CONFIG"
  --set devicePlugin.config.default="$DEFAULT_CONFIG_KEY"
  --set mps.root="$MPS_ROOT"
  # DCGM and migManager are off until Phase 5-B observability work.
  --set dcgmExporter.enabled=false
  --set migManager.enabled=false
  --set nodeStatusExporter.enabled=false
  # Validator's CUDA workload is useful as a smoke test; leave on by default.
  --set 'validator.plugin.env[0].name=WITH_WORKLOAD'
  --set 'validator.plugin.env[0].value=true'
)
[[ -n "$DRY_RUN" ]] && helm_args+=(--dry-run)

helm "${helm_args[@]}"

log "done. verify with: kubectl -n $NAMESPACE get pods"
log "  expected DaemonSets: nvidia-device-plugin-daemonset, nvidia-mps-control-daemon-* (when sharing.mps active),"
log "    gpu-feature-discovery-*, gpu-operator (Deployment), node-feature-discovery-*"
