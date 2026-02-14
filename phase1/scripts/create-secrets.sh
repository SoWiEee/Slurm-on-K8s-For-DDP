#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=${1:-slurm}
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

ssh-keygen -t ed25519 -N '' -f "$WORKDIR/id_ed25519" >/dev/null
openssl rand -base64 1024 | tr -d '\n' | head -c 1024 > "$WORKDIR/munge.key"

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret generic slurm-munge-key \
  --from-file=munge.key="$WORKDIR/munge.key" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NAMESPACE" create secret generic slurm-ssh-key \
  --from-file=id_ed25519="$WORKDIR/id_ed25519" \
  --from-file=id_ed25519.pub="$WORKDIR/id_ed25519.pub" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secrets created in namespace: $NAMESPACE"
