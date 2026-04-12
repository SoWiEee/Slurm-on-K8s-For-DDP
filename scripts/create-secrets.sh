#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-slurm}"
REGENERATE_SECRETS="${REGENERATE_SECRETS:-false}"

require_tool() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "[create-secrets] $1 is required" >&2
    exit 1
  }
}

log() {
  echo "[create-secrets] $*"
}

require_tool kubectl
require_tool ssh-keygen
require_tool openssl

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

need_munge=true
need_ssh=true
need_jwt=true

if [[ "$REGENERATE_SECRETS" != "true" ]]; then
  if kubectl -n "$NAMESPACE" get secret slurm-munge-key >/dev/null 2>&1; then
    need_munge=false
  fi
  if kubectl -n "$NAMESPACE" get secret slurm-ssh-key >/dev/null 2>&1; then
    need_ssh=false
  fi
  if kubectl -n "$NAMESPACE" get secret slurm-jwt-secret >/dev/null 2>&1; then
    need_jwt=false
  fi
fi

if [[ "$need_munge" == "true" ]]; then
  log "generating munge key..."
  openssl rand -out "$WORKDIR/munge.key" 1024

  kubectl -n "$NAMESPACE" delete secret slurm-munge-key --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" create secret generic slurm-munge-key \
    --from-file=munge.key="$WORKDIR/munge.key" >/dev/null

  log "created secret slurm-munge-key"
else
  log "keeping existing secret slurm-munge-key"
fi

if [[ "$need_ssh" == "true" ]]; then
  log "generating ssh keypair..."
  ssh-keygen -t ed25519 -N '' -f "$WORKDIR/id_ed25519" >/dev/null

  kubectl -n "$NAMESPACE" delete secret slurm-ssh-key --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" create secret generic slurm-ssh-key \
    --from-file=id_ed25519="$WORKDIR/id_ed25519" \
    --from-file=id_ed25519.pub="$WORKDIR/id_ed25519.pub" >/dev/null

  log "created secret slurm-ssh-key"
else
  log "keeping existing secret slurm-ssh-key"
fi

if [[ "$need_jwt" == "true" ]]; then
  log "generating JWT HS256 key..."
  openssl rand 32 > "$WORKDIR/jwt_hs256.key"

  kubectl -n "$NAMESPACE" delete secret slurm-jwt-secret --ignore-not-found >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" create secret generic slurm-jwt-secret \
    --from-file=jwt_hs256.key="$WORKDIR/jwt_hs256.key" >/dev/null

  log "created secret slurm-jwt-secret"
else
  log "keeping existing secret slurm-jwt-secret"
fi

log "done"