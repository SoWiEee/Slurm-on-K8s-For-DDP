#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=${1:-slurm}
REGENERATE_SECRETS=${REGENERATE_SECRETS:-false}
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

changed=false

if [[ "$REGENERATE_SECRETS" == "true" ]] || ! kubectl -n "$NAMESPACE" get secret slurm-munge-key >/dev/null 2>&1; then
  python3 - <<'PY_CREATE_MUNGE' > "$WORKDIR/munge.key"
import base64, os, sys
# 768 random bytes become exactly 1024 base64 characters, avoiding SIGPIPE-prone shell pipelines.
sys.stdout.write(base64.b64encode(os.urandom(768)).decode())
PY_CREATE_MUNGE
  kubectl -n "$NAMESPACE" create secret generic slurm-munge-key \
    --from-file=munge.key="$WORKDIR/munge.key" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "slurm-munge-key: applied"
  changed=true
else
  echo "slurm-munge-key: unchanged"
fi

if [[ "$REGENERATE_SECRETS" == "true" ]] || ! kubectl -n "$NAMESPACE" get secret slurm-ssh-key >/dev/null 2>&1; then
  ssh-keygen -t ed25519 -N '' -f "$WORKDIR/id_ed25519" >/dev/null
  kubectl -n "$NAMESPACE" create secret generic slurm-ssh-key \
    --from-file=id_ed25519="$WORKDIR/id_ed25519" \
    --from-file=id_ed25519.pub="$WORKDIR/id_ed25519.pub" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "slurm-ssh-key: applied"
  changed=true
else
  echo "slurm-ssh-key: unchanged"
fi

if [[ "$changed" == "true" ]]; then
  echo "Secrets changed in namespace: $NAMESPACE"
else
  echo "Secrets unchanged in namespace: $NAMESPACE"
fi
