#!/usr/bin/env bash
# deploy-1.sh - Linux + k3s + GPU prerequisite deployment.
#
# This consolidates README deployment steps 1-4 for the current target
# environment. It intentionally does not support Kind.
#
# What it does:
#   1. Validate host prerequisites: Linux, NVIDIA driver, Docker, k3s, kubectl, Helm.
#   2. Build core container images.
#   3. Import those images into k3s containerd.
#   4. Create Slurm secrets.
#   5. Apply NVIDIA RuntimeClass and the Slurm accounting backend.
#
# Common knobs:
#   SKIP_BUILD=1          Skip docker build.
#   SKIP_IMPORT=1         Skip k3s ctr image import.
#   SKIP_SECRETS=1        Skip secret creation.
#   REGENERATE_SECRETS=true Recreate munge/ssh/JWT secrets even if present.
#   SKIP_PREREQS=1        Skip RuntimeClass/accounting apply.
#   NAMESPACE=slurm       Target namespace for secrets/accounting.
#   KUBECONFIG=...        Defaults to ~/.kube/config if present, otherwise k3s default.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-slurm}"
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
export KUBECONFIG

SKIP_BUILD="${SKIP_BUILD:-0}"
SKIP_IMPORT="${SKIP_IMPORT:-0}"
SKIP_SECRETS="${SKIP_SECRETS:-0}"
SKIP_PREREQS="${SKIP_PREREQS:-0}"
REGENERATE_SECRETS="${REGENERATE_SECRETS:-false}"
SECRET_WORKDIR=""

IMAGES=(
  "slurm-controller:latest|docker/controller/Dockerfile|docker/controller"
  "slurm-worker:latest|docker/worker/Dockerfile|docker/worker"
  "slurm-elastic-operator:latest|docker/operator/Dockerfile|."
  "slurm-exporter:latest|docker/slurm-exporter/Dockerfile|docker/slurm-exporter"
)

log() { printf '[%(%Y-%m-%dT%H:%M:%S%z)T] [deploy-1] %s\n' -1 "$*"; }
warn() { printf '[%(%Y-%m-%dT%H:%M:%S%z)T] [deploy-1][WARN] %s\n' -1 "$*" >&2; }
fail() { printf '[%(%Y-%m-%dT%H:%M:%S%z)T] [deploy-1][ERROR] %s\n' -1 "$*" >&2; exit 1; }

cleanup() {
  if [[ -n "${SECRET_WORKDIR:-}" ]]; then
    rm -rf "$SECRET_WORKDIR"
  fi
}
trap cleanup EXIT

run() {
  log "+ $*"
  "$@"
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

sudo_cmd() {
  if [[ "${EUID}" -eq 0 ]]; then
    "$@"
  else
    sudo "$@"
  fi
}

ensure_kubeconfig() {
  if [[ -r "$KUBECONFIG" ]]; then
    log "using KUBECONFIG=$KUBECONFIG"
    return
  fi

  if sudo_cmd test -r /etc/rancher/k3s/k3s.yaml; then
    log "copying /etc/rancher/k3s/k3s.yaml to $KUBECONFIG"
    mkdir -p "$(dirname "$KUBECONFIG")"
    sudo_cmd cp /etc/rancher/k3s/k3s.yaml "$KUBECONFIG"
    sudo_cmd chown "$(id -u):$(id -g)" "$KUBECONFIG"
    chmod 600 "$KUBECONFIG"
    return
  fi

  fail "KUBECONFIG is not readable and /etc/rancher/k3s/k3s.yaml is unavailable. Run scripts/setup-linux-gpu.sh --k3s first or set KUBECONFIG."
}

check_prereqs() {
  log "checking Linux + k3s + GPU prerequisites"
  [[ "$(uname -s)" == "Linux" ]] || fail "this deployment script supports Linux only"

  require_cmd docker
  require_cmd kubectl
  require_cmd helm
  require_cmd k3s
  require_cmd nvidia-smi
  require_cmd openssl
  require_cmd ssh-keygen

  ensure_kubeconfig

  log "NVIDIA GPU status"
  nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || fail "nvidia-smi failed"

  log "k3s version: $(k3s --version | head -1)"
  log "helm version: $(helm version --short)"
  run kubectl get nodes -o wide
}

build_images() {
  if [[ "$SKIP_BUILD" == "1" ]]; then
    warn "SKIP_BUILD=1; skipping docker build"
    return
  fi

  log "building core container images"
  local item image dockerfile context
  for item in "${IMAGES[@]}"; do
    IFS='|' read -r image dockerfile context <<<"$item"
    run docker build -t "$image" -f "$ROOT_DIR/$dockerfile" "$ROOT_DIR/$context"
  done
}

import_images() {
  if [[ "$SKIP_IMPORT" == "1" ]]; then
    warn "SKIP_IMPORT=1; skipping k3s image import"
    return
  fi

  log "importing images into k3s containerd"
  local item image
  for item in "${IMAGES[@]}"; do
    IFS='|' read -r image _ _ <<<"$item"
    log "importing $image"
    docker save "$image" | sudo_cmd k3s ctr images import -
  done
}

secret_exists() {
  kubectl -n "$NAMESPACE" get secret "$1" >/dev/null 2>&1
}

create_or_keep_secret() {
  local name="$1"
  local description="$2"
  shift 2

  if [[ "$REGENERATE_SECRETS" != "true" ]] && secret_exists "$name"; then
    log "keeping existing secret $name"
    return
  fi

  log "creating $description secret: $name"
  kubectl -n "$NAMESPACE" delete secret "$name" --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" create secret generic "$name" "$@" >/dev/null
  log "created secret $name"
}

create_secrets() {
  if [[ "$SKIP_SECRETS" == "1" ]]; then
    warn "SKIP_SECRETS=1; skipping secret creation"
    return
  fi

  log "creating namespace and Slurm secrets in namespace=$NAMESPACE"
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

  local workdir
  SECRET_WORKDIR="$(mktemp -d)"
  workdir="$SECRET_WORKDIR"

  log "generating temporary secret material"
  openssl rand -out "$workdir/munge.key" 1024
  ssh-keygen -t ed25519 -N '' -f "$workdir/id_ed25519" >/dev/null
  openssl rand 32 > "$workdir/jwt_hs256.key"

  create_or_keep_secret slurm-munge-key "Munge" \
    --from-file=munge.key="$workdir/munge.key"
  create_or_keep_secret slurm-ssh-key "SSH host keypair" \
    --from-file=id_ed25519="$workdir/id_ed25519" \
    --from-file=id_ed25519.pub="$workdir/id_ed25519.pub"
  create_or_keep_secret slurm-jwt-secret "JWT HS256" \
    --from-file=jwt_hs256.key="$workdir/jwt_hs256.key"
}

apply_prereqs() {
  if [[ "$SKIP_PREREQS" == "1" ]]; then
    warn "SKIP_PREREQS=1; skipping RuntimeClass/accounting apply"
    return
  fi

  log "applying NVIDIA RuntimeClass"
  run kubectl apply -f "$ROOT_DIR/manifests/gpu/runtime-class.yaml"

  log "applying Slurm accounting backend"
  run kubectl apply -f "$ROOT_DIR/manifests/core/slurm-accounting.yaml"

  log "waiting for accounting backend rollout"
  run kubectl -n "$NAMESPACE" rollout status statefulset/mysql --timeout=180s
  run kubectl -n "$NAMESPACE" rollout status deployment/slurmdbd --timeout=180s
}

summary() {
  log "deployment step 1-4 complete"
  log "next README step: install/upgrade Helm chart"
  printf '\nUseful checks:\n'
  printf '  export KUBECONFIG=%q\n' "$KUBECONFIG"
  printf '  kubectl -n %q get pods\n' "$NAMESPACE"
  printf '  helm install slurm-platform ./chart -f chart/values-k3s.yaml -n %q --create-namespace\n' "$NAMESPACE"
}

main() {
  cd "$ROOT_DIR"
  check_prereqs
  build_images
  import_images
  create_secrets
  apply_prereqs
  summary
}

main "$@"
