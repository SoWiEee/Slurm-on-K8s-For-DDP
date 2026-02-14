#!/usr/bin/env bash
set -euo pipefail

MUNGE_KEY=/etc/munge/munge.key

if [[ ! -f "$MUNGE_KEY" ]]; then
  echo "[worker] missing $MUNGE_KEY" >&2
  exit 1
fi

install -d -m 0700 -o munge -g munge /etc/munge /run/munge /var/lib/munge /var/log/munge
chmod 0400 "$MUNGE_KEY"
chown munge:munge "$MUNGE_KEY"

install -d -m 0700 /root/.ssh
if [[ -f /root/.ssh/id_ed25519.pub ]]; then
  cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys
  chmod 0600 /root/.ssh/authorized_keys
fi

ssh-keygen -A
/usr/sbin/munged --syslog
sleep 1
if ! pgrep -x munged >/dev/null; then
  echo "[worker] munged failed to start" >&2
  exit 1
fi

/usr/sbin/sshd

install -d -m 0755 /var/spool/slurmd /var/log/slurm /run/slurmd
exec slurmd -Dvvv -N "$(hostname)"
