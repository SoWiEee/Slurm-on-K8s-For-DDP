#!/usr/bin/env bash
set -euo pipefail

# Run this script inside Ubuntu WSL2/VM with root privileges.
NFS_EXPORT_PATH=${NFS_EXPORT_PATH:-/srv/nfs/k8s}
NFS_EXPORT_CIDR=${NFS_EXPORT_CIDR:-172.16.0.0/12}
NFS_EXPORT_OPTIONS=${NFS_EXPORT_OPTIONS:-rw,sync,no_subtree_check,no_root_squash,insecure}
# For troubleshooting only: export to all clients, then tighten CIDR after validation.
NFS_EXPORT_ALLOW_ALL_DEBUG=${NFS_EXPORT_ALLOW_ALL_DEBUG:-false}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo bash phase3/scripts/setup-nfs-server.sh" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-kernel-server nfs-common rpcbind

mkdir -p "$NFS_EXPORT_PATH"
chown nobody:nogroup "$NFS_EXPORT_PATH"
chmod 0777 "$NFS_EXPORT_PATH"

exports_file=/etc/exports
backup_file="/etc/exports.bak.$(date +%Y%m%d%H%M%S)"
cp "$exports_file" "$backup_file"

if [[ "$NFS_EXPORT_ALLOW_ALL_DEBUG" == "true" ]]; then
  export_target='*'
else
  export_target="$NFS_EXPORT_CIDR"
fi

# Replace existing exports for same path to avoid stale/restrictive old CIDR lines.
tmp_exports=$(mktemp)
awk -v path="$NFS_EXPORT_PATH" '$1 != path { print $0 }' "$exports_file" > "$tmp_exports"
echo "${NFS_EXPORT_PATH} ${export_target}(${NFS_EXPORT_OPTIONS})" >> "$tmp_exports"
cat "$tmp_exports" > "$exports_file"
rm -f "$tmp_exports"

exportfs -ra
systemctl enable --now rpcbind || true
systemctl enable --now nfs-server || systemctl enable --now nfs-kernel-server

echo "NFS server ready"
echo "Export path: ${NFS_EXPORT_PATH}"
if [[ "$NFS_EXPORT_ALLOW_ALL_DEBUG" == "true" ]]; then
  echo "Allowed target: * (debug mode)"
else
  echo "Allowed CIDR: ${NFS_EXPORT_CIDR}"
fi
echo "Export options: ${NFS_EXPORT_OPTIONS}"
echo "Backup exports: ${backup_file}"
echo "Current exports:"
exportfs -v
