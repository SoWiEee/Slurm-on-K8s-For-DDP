#!/usr/bin/env bash
# add-ssh-key.sh — Add or remove an external user public key from the
# slurm-login pod (Phase 7-B SSH Login).
#
# Usage:
#   bash scripts/add-ssh-key.sh add    "ssh-ed25519 AAAA... user@host"
#   bash scripts/add-ssh-key.sh remove "ssh-ed25519 AAAA... user@host"
#   bash scripts/add-ssh-key.sh list
#
# The script edits chart/values.yaml in-place, then runs helm upgrade
# to apply the change. Run from the repo root.

set -euo pipefail

CHART_DIR="chart"
VALUES_FILE="$CHART_DIR/values.yaml"
RELEASE="${HELM_RELEASE:-slurm-platform}"
NAMESPACE="${HELM_NAMESPACE:-slurm}"

usage() {
  echo "Usage: $0 add <pubkey> | remove <pubkey> | list"
  exit 1
}

[[ $# -lt 1 ]] && usage

CMD="$1"
KEY="${2:-}"

current_keys() {
  python3 - "$VALUES_FILE" <<'EOF'
import sys, re
txt = open(sys.argv[1]).read()
m = re.search(r'login:\n  ssh:\n(?:.*\n)*?    authorizedKeys: \|?\n((?:      .*\n)*)', txt)
if m:
    for line in m.group(1).splitlines():
        stripped = line.strip()
        if stripped:
            print(stripped)
EOF
}

set_keys() {
  local keys_block="$1"
  python3 - "$VALUES_FILE" "$keys_block" <<'EOF'
import sys, re
path, new_block = sys.argv[1], sys.argv[2]
txt = open(path).read()

if new_block.strip():
    replacement = 'login:\n  ssh:\n    nodePort: 2222\n    authorizedKeys: |\n'
    for line in new_block.strip().splitlines():
        replacement += f'      {line}\n'
else:
    replacement = 'login:\n  ssh:\n    nodePort: 2222\n    authorizedKeys: ""\n'

txt = re.sub(
    r'login:\n  ssh:\n    nodePort: \d+\n    authorizedKeys:.*?(?=\n\S|\Z)',
    replacement.rstrip('\n'),
    txt,
    flags=re.DOTALL,
)
open(path, 'w').write(txt)
EOF
}

case "$CMD" in
  list)
    echo "Current authorized keys:"
    current_keys | nl -ba || echo "(none)"
    ;;
  add)
    [[ -z "$KEY" ]] && usage
    existing=$(current_keys)
    if echo "$existing" | grep -qF "$KEY"; then
      echo "Key already present — nothing to do."
      exit 0
    fi
    new_block="${existing:+$existing$'\n'}$KEY"
    set_keys "$new_block"
    echo "Key added. Running helm upgrade..."
    helm upgrade "$RELEASE" "$CHART_DIR" --namespace "$NAMESPACE" --reuse-values -f "$VALUES_FILE"
    echo "Done. Connect with: ssh -p 30022 root@<k3s-host-ip>"
    ;;
  remove)
    [[ -z "$KEY" ]] && usage
    existing=$(current_keys)
    new_block=$(echo "$existing" | grep -vF "$KEY" || true)
    set_keys "$new_block"
    echo "Key removed. Running helm upgrade..."
    helm upgrade "$RELEASE" "$CHART_DIR" --namespace "$NAMESPACE" --reuse-values -f "$VALUES_FILE"
    echo "Done."
    ;;
  *)
    usage
    ;;
esac
