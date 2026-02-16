#!/usr/bin/env bash
set -euo pipefail

# Run this script inside Ubuntu WSL2/VM with root privileges.
NFS_EXPORT_PATH=${NFS_EXPORT_PATH:-/srv/nfs/k8s}
NFS_EXPORT_CIDR=${NFS_EXPORT_CIDR:-172.16.0.0/12}

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root: sudo bash phase3/scripts/setup-nfs-server.sh" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-kernel-server

mkdir -p "$NFS_EXPORT_PATH"
chown nobody:nogroup "$NFS_EXPORT_PATH"
chmod 0777 "$NFS_EXPORT_PATH"

if ! grep -q "${NFS_EXPORT_PATH}" /etc/exports; then
  echo "${NFS_EXPORT_PATH} ${NFS_EXPORT_CIDR}(rw,sync,no_subtree_check,no_root_squash)" >> /etc/exports
fi

exportfs -ra
systemctl enable --now nfs-server || systemctl enable --now nfs-kernel-server

echo "NFS server ready"
echo "Export path: ${NFS_EXPORT_PATH}"
echo "Allowed CIDR: ${NFS_EXPORT_CIDR}"
echo "Check with: exportfs -v"
