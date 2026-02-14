#!/usr/bin/env bash
set -euo pipefail

MUNGE_SRC=/slurm-secrets/munge.key
MUNGE_DST=/etc/munge/munge.key
SSH_SRC_DIR=/slurm-secrets

if [[ ! -f "$MUNGE_SRC" ]]; then
  echo "[controller] missing $MUNGE_SRC" >&2
  exit 1
fi

# Prepare writable runtime locations and enforce secure munge ownership/permissions.
for d in /etc/munge /run/munge /var/lib/munge /var/log/munge; do
  mkdir -p "$d"
  chown munge:munge "$d"
  chmod 0700 "$d"
done

cp "$MUNGE_SRC" "$MUNGE_DST"
chown munge:munge "$MUNGE_DST"
chmod 0400 "$MUNGE_DST"

install -d -m 0700 /root/.ssh
if [[ -f "$SSH_SRC_DIR/id_ed25519" ]]; then
  cp "$SSH_SRC_DIR/id_ed25519" /root/.ssh/id_ed25519
  chmod 0600 /root/.ssh/id_ed25519
fi
if [[ -f "$SSH_SRC_DIR/id_ed25519.pub" ]]; then
  cp "$SSH_SRC_DIR/id_ed25519.pub" /root/.ssh/id_ed25519.pub
  chmod 0644 /root/.ssh/id_ed25519.pub
  cat /root/.ssh/id_ed25519.pub >> /root/.ssh/authorized_keys
  chmod 0600 /root/.ssh/authorized_keys
fi

ssh-keygen -A
/usr/sbin/munged --syslog
sleep 1
if ! pgrep -x munged >/dev/null; then
  echo "[controller] munged failed to start" >&2
  ls -ld /var/lib/munge /run/munge /etc/munge >&2 || true
  exit 1
fi

/usr/sbin/sshd

install -d -m 0755 /var/spool/slurmctld /var/log/slurm
exec slurmctld -Dvvv
