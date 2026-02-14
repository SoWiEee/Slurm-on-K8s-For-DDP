#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f /etc/munge/munge.key ]]; then
  echo "[worker] missing /etc/munge/munge.key" >&2
  exit 1
fi

chmod 400 /etc/munge/munge.key
chown munge:munge /etc/munge/munge.key
mkdir -p /var/run/munge /var/log
chown munge:munge /var/run/munge

if [[ -f /root/.ssh/id_ed25519.pub ]]; then
  cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi

ssh-keygen -A
/usr/sbin/munged --foreground --verbose &
/usr/sbin/sshd

mkdir -p /var/spool/slurmd /var/log/slurm
slurmd -Dvvv -N "$(hostname)"
