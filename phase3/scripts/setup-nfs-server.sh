#!/usr/bin/env bash
set -euo pipefail

# Run this script inside Ubuntu (WSL2/VM) with root privileges.
#
# Notes for WSL2:
# - If systemd is disabled, this script falls back to `service`.
# - Your Kind/Docker containers must be able to reach NFS_SERVER IP/hostname you pass to bootstrap-phase3.sh.

NFS_EXPORT_PATH=${NFS_EXPORT_PATH:-/srv/nfs/k8s}

# Allowed clients for /etc/exports.
# For a quick local dev setup you can use "*".
# If you prefer a CIDR, set e.g. "172.16.0.0/12" or your Docker/Kind bridge subnet.
NFS_EXPORT_CLIENTS=${NFS_EXPORT_CLIENTS:-172.16.0.0/12}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo bash phase3/scripts/setup-nfs-server.sh" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-kernel-server

mkdir -p "$NFS_EXPORT_PATH"
chown nobody:nogroup "$NFS_EXPORT_PATH"
chmod 0777 "$NFS_EXPORT_PATH"

exports_line="${NFS_EXPORT_PATH} ${NFS_EXPORT_CLIENTS}(rw,sync,no_subtree_check,no_root_squash)"

if ! grep -qE "^\s*${NFS_EXPORT_PATH}\s" /etc/exports; then
  echo "$exports_line" >> /etc/exports
else
  # Replace existing line for this export path (best-effort)
  sed -i -E "s|^\s*${NFS_EXPORT_PATH}\s.*|${exports_line}|g" /etc/exports
fi

exportfs -ra

if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files >/dev/null 2>&1; then
  systemctl enable --now nfs-server || systemctl enable --now nfs-kernel-server
else
  service nfs-kernel-server restart
fi

echo "NFS server ready"
echo "Export path: ${NFS_EXPORT_PATH}"
echo "Allowed clients: ${NFS_EXPORT_CLIENTS}"
echo "Check with: exportfs -v"
