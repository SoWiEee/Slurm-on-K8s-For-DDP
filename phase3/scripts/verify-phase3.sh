#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
VERIFY_TIMEOUT=${VERIFY_TIMEOUT:-180s}
L1_RETRY_COUNT=${L1_RETRY_COUNT:-3}
L1_RETRY_INTERVAL_SECONDS=${L1_RETRY_INTERVAL_SECONDS:-10}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

# Layer 1: base connectivity + scheduling visibility.
bash phase1/scripts/verify-phase1.sh

# Keep two worker pods ready for phase3 checks.
kubectl -n "$NAMESPACE" scale statefulset/slurm-worker --replicas=2
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$VERIFY_TIMEOUT"
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-0 --timeout="$VERIFY_TIMEOUT"
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-1 --timeout="$VERIFY_TIMEOUT"

# Explicitly target existing worker pods to avoid stale slurm node entries (e.g. worker-2)
# causing DNS resolution failures in srun.
worker_nodelist="slurm-worker-0,slurm-worker-1"

kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "
  getent hosts slurm-worker-0.slurm-worker.slurm.svc.cluster.local >/dev/null
  getent hosts slurm-worker-1.slurm-worker.slurm.svc.cluster.local >/dev/null
"

# Recover transient slurmd states before L1 srun (e.g. COMPLETING/NOT_RESPONDING).
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "
  scontrol update nodename=slurm-worker-0 state=resume || true
  scontrol update nodename=slurm-worker-1 state=resume || true
"

l1_ok=false
for attempt in $(seq 1 "$L1_RETRY_COUNT"); do
  set +e
  unique_hosts=$(kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "srun --nodelist=${worker_nodelist} -N 2 -n 2 bash -lc 'hostname' | sort -u | wc -l")
  srun_rc=$?
  set -e

  if [[ "$srun_rc" -eq 0 && "$unique_hosts" -ge 2 ]]; then
    l1_ok=true
    break
  fi

  echo "[L1] srun attempt ${attempt}/${L1_RETRY_COUNT} failed (rc=${srun_rc}, unique_hosts=${unique_hosts:-0}), retrying..." >&2
  kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc 'sinfo; scontrol show nodes | sed -n "1,120p"' >&2 || true
  sleep "$L1_RETRY_INTERVAL_SECONDS"
done

if [[ "$l1_ok" != "true" ]]; then
  echo "[L1] expected 2 hosts from srun on ${worker_nodelist}, got ${unique_hosts:-0}" >&2
  exit 1
fi

echo "[L1] srun cross-worker execution verified (unique_hosts=${unique_hosts}, nodelist=${worker_nodelist})."

# Layer 2: data consistency through shared volume.
kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- bash -s <<'SCRIPT'
set -euo pipefail
mkdir -p /shared/checkpoints
date +%s > /shared/checkpoints/phase3_meta.txt
sha256sum /shared/checkpoints/phase3_meta.txt > /shared/checkpoints/phase3_meta.sha256
SCRIPT

k_worker1=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- bash -lc "sha256sum /shared/checkpoints/phase3_meta.txt | awk '{print \$1}'")
k_origin=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- bash -lc "awk '{print \$1}' /shared/checkpoints/phase3_meta.sha256")

if [[ "$k_worker1" != "$k_origin" ]]; then
  echo "[L2] checksum mismatch between workers" >&2
  exit 1
fi

mtime_before=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- stat -c %Y /shared/checkpoints/phase3_meta.txt)
sleep 2
kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- bash -lc 'echo "update-$(date +%s)" >> /shared/checkpoints/phase3_meta.txt'
mtime_after=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- stat -c %Y /shared/checkpoints/phase3_meta.txt)

if (( mtime_after <= mtime_before )); then
  echo "[L2] mtime did not increase after update" >&2
  exit 1
fi

kubectl -n "$NAMESPACE" delete pod slurm-worker-1 --wait=true
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-1 --timeout="$VERIFY_TIMEOUT"
kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- bash -lc 'test -f /shared/checkpoints/phase3_meta.txt'

echo "[L2] shared checkpoint consistency verified (checksum/mtime/path/restart)."

# Layer 3: training semantics (checkpoint/resume continuity).
kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- bash -s <<'SCRIPT'
set -euo pipefail
mkdir -p /shared/scripts /shared/checkpoints
cat > /shared/scripts/mock-train.sh <<'INNER'
#!/usr/bin/env bash
set -euo pipefail
STATE_FILE=/shared/checkpoints/mock_train.state
LOG_FILE=/shared/checkpoints/mock_train.log
STEPS=${1:-3}
HOST=$(hostname)

if [[ -f "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE"
else
  EPOCH=0
  STEP=0
  OPT=100
fi

for _ in $(seq 1 "$STEPS"); do
  STEP=$((STEP + 1))
  OPT=$((OPT + 7))
  LOSS=$(awk -v s="$STEP" 'BEGIN { printf "%.6f", 10/(s+10) }')
  printf "host=%s step=%d opt=%d loss=%s\n" "$HOST" "$STEP" "$OPT" "$LOSS" >> "$LOG_FILE"
  {
    echo "EPOCH=$EPOCH"
    echo "STEP=$STEP"
    echo "OPT=$OPT"
  } > "${STATE_FILE}.tmp"
  mv "${STATE_FILE}.tmp" "$STATE_FILE"
done
INNER
chmod +x /shared/scripts/mock-train.sh
rm -f /shared/checkpoints/mock_train.state /shared/checkpoints/mock_train.log
SCRIPT

kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- bash -lc '/shared/scripts/mock-train.sh 4'
pre_step=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- bash -lc "source /shared/checkpoints/mock_train.state && echo \$STEP")

kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- bash -lc '/shared/scripts/mock-train.sh 3'
final_step=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- bash -lc "source /shared/checkpoints/mock_train.state && echo \$STEP")

if [[ "$final_step" -ne $((pre_step + 3)) ]]; then
  echo "[L3] resume step continuity failed: pre=${pre_step}, final=${final_step}" >&2
  exit 1
fi

continuity_ok=$(kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- awk '
BEGIN {prev_step=0; prev_opt=0; prev_loss=0; ok=1}
{
  split($2,a,"="); split($3,b,"="); split($4,c,"=");
  step=a[2]+0; opt=b[2]+0; loss=c[2]+0;
  if (NR>1 && step != prev_step + 1) ok=0;
  if (NR>1 && opt <= prev_opt) ok=0;
  if (NR>1 && loss >= prev_loss) ok=0;
  prev_step=step; prev_opt=opt; prev_loss=loss;
}
END {print ok}
' /shared/checkpoints/mock_train.log)

if [[ "$continuity_ok" != "1" ]]; then
  echo "[L3] loss/step/optimizer continuity validation failed" >&2
  kubectl -n "$NAMESPACE" exec pod/slurm-worker-1 -- cat /shared/checkpoints/mock_train.log >&2
  exit 1
fi

echo "[L3] checkpoint/resume semantics verified (step/loss/optimizer continuity)."

echo "Phase 3 verification passed."
