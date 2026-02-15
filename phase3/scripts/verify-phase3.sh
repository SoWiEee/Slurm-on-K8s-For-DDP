#!/usr/bin/env bash
set -Eeuo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
VERIFY_TIMEOUT=${VERIFY_TIMEOUT:-180s}
L1_RETRY_COUNT=${L1_RETRY_COUNT:-3}
L1_RETRY_INTERVAL_SECONDS=${L1_RETRY_INTERVAL_SECONDS:-10}
DISABLE_OPERATOR_DURING_VERIFY=${DISABLE_OPERATOR_DURING_VERIFY:-true}
WORKER_TARGET_REPLICAS=${WORKER_TARGET_REPLICAS:-2}
WORKER_READY_MAX_WAIT_SECONDS=${WORKER_READY_MAX_WAIT_SECONDS:-240}
SLURM_NODE_READY_MAX_WAIT_SECONDS=${SLURM_NODE_READY_MAX_WAIT_SECONDS:-240}

operator_original_replicas=""
operator_scaled_down="false"

pod_exists() {
  local pod_name=$1
  kubectl -n "$NAMESPACE" get pod "$pod_name" >/dev/null 2>&1
}

dump_pod_diagnostics() {
  local pod_name=$1
  echo "--- ${pod_name} describe/logs (if exists) ---"
  if ! pod_exists "$pod_name"; then
    echo "[phase3/verify] ${pod_name} not found (skip)."
    return 0
  fi

  kubectl -n "$NAMESPACE" describe pod "$pod_name" || true
  kubectl -n "$NAMESPACE" logs pod/"$pod_name" --tail=120 || true

  # `--previous` only works when a terminated previous container exists.
  if kubectl -n "$NAMESPACE" get pod "$pod_name" -o jsonpath='{.status.containerStatuses[0].lastState.terminated.exitCode}' 2>/dev/null | grep -Eq '^[0-9]+$'; then
    kubectl -n "$NAMESPACE" logs pod/"$pod_name" --previous --tail=120 || true
  else
    echo "[phase3/verify] ${pod_name} has no previous terminated container (skip --previous)."
  fi
}

dump_verify_diagnostics() {
  {
    echo "[phase3/verify] ===== diagnostics start ====="
    echo "--- context ---"
    kubectl config current-context || true
    echo
    echo "--- pods ---"
    kubectl -n "$NAMESPACE" get pods -o wide || true
    echo
    echo "--- statefulsets ---"
    kubectl -n "$NAMESPACE" get sts slurm-controller slurm-worker -o wide || true
    echo
    echo "--- operator deployment ---"
    kubectl -n "$NAMESPACE" get deployment slurm-elastic-operator -o wide || true
    echo
    echo "--- endpoints ---"
    kubectl -n "$NAMESPACE" get endpoints slurm-worker slurm-controller -o wide || true
    echo
    echo "--- recent events ---"
    kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -n 120 || true
    echo
    echo "--- slurm controller view (sinfo/scontrol) ---"
    kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc 'sinfo; scontrol show nodes' || true
    echo
    echo "--- DNS checks in controller ---"
    kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc '
      getent hosts slurm-worker-0.slurm-worker.slurm.svc.cluster.local || true
      getent hosts slurm-worker-1.slurm-worker.slurm.svc.cluster.local || true
      getent hosts slurm-worker-2.slurm-worker.slurm.svc.cluster.local || true
      getent hosts slurm-controller-0.slurm-controller.slurm.svc.cluster.local || true
    ' || true
    echo
    dump_pod_diagnostics "slurm-worker-0"
    echo
    dump_pod_diagnostics "slurm-worker-1"
    echo
    dump_pod_diagnostics "slurm-worker-2"
    echo "[phase3/verify] ===== diagnostics end ====="
  } >&2
}

restore_operator() {
  if [[ "$operator_scaled_down" == "true" && -n "$operator_original_replicas" ]]; then
    echo "[phase3/verify] restoring operator replicas to ${operator_original_replicas}" >&2
    kubectl -n "$NAMESPACE" scale deployment/slurm-elastic-operator --replicas="$operator_original_replicas" >/dev/null || true
    kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$VERIFY_TIMEOUT" >/dev/null || true
  fi
}

on_error() {
  local exit_code=$?
  echo "[phase3/verify] failed (exit=${exit_code}), dumping diagnostics..." >&2
  dump_verify_diagnostics
  exit "$exit_code"
}
trap on_error ERR
trap restore_operator EXIT


wait_slurm_nodes_ready() {
  local deadline=$(( $(date +%s) + SLURM_NODE_READY_MAX_WAIT_SECONDS ))

  while (( $(date +%s) < deadline )); do
    local status_lines bad_nodes=""
    status_lines=$(kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "sinfo -Nh -n ${worker_nodelist} -o '%N %T'" || true)

    if [[ -n "$status_lines" ]]; then
      while IFS= read -r line; do
        [[ -z "$line" ]] && continue
        local n st
        n=$(awk '{print $1}' <<<"$line")
        st=$(awk '{print $2}' <<<"$line")
        if [[ "$st" != "idle" && "$st" != "mix" && "$st" != "allocated" ]]; then
          bad_nodes+="${n}:${st} "
        fi
      done <<<"$status_lines"
    fi

    if [[ -z "$bad_nodes" ]]; then
      return 0
    fi

    echo "[phase3/verify] waiting Slurm node readiness, unhealthy=[$bad_nodes]" >&2

    kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "
      for n in slurm-worker-0 slurm-worker-1; do
        scontrol update NodeName=\$n State=UNDRAIN >/dev/null 2>&1 || true
        scontrol update NodeName=\$n State=RESUME >/dev/null 2>&1 || true
      done
    " || true

    for n in slurm-worker-0 slurm-worker-1; do
      if grep -q "${n}:" <<<"$bad_nodes"; then
        echo "[phase3/verify] recycling pod ${n} due to Slurm state issue" >&2
        kubectl -n "$NAMESPACE" delete pod "$n" --wait=true >/dev/null 2>&1 || true
      fi
    done

    kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-0 --timeout="$VERIFY_TIMEOUT" || true
    kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-1 --timeout="$VERIFY_TIMEOUT" || true
    sleep 5
  done

  echo "[phase3/verify] Slurm nodes not schedulable in time (${worker_nodelist})" >&2
  return 1
}

wait_worker_replicas_ready() {
  local desired=$1
  local deadline=$(( $(date +%s) + WORKER_READY_MAX_WAIT_SECONDS ))

  while (( $(date +%s) < deadline )); do
    local spec ready
    spec=$(kubectl -n "$NAMESPACE" get sts slurm-worker -o jsonpath='{.spec.replicas}')
    ready=$(kubectl -n "$NAMESPACE" get sts slurm-worker -o jsonpath='{.status.readyReplicas}')
    ready=${ready:-0}

    if [[ "$spec" != "$desired" ]]; then
      echo "[phase3/verify] detected slurm-worker spec.replicas=${spec} (expected ${desired}); scaling back..." >&2
      kubectl -n "$NAMESPACE" scale statefulset/slurm-worker --replicas="$desired" >/dev/null || true
    fi

    if [[ "$spec" == "$desired" && "$ready" -ge "$desired" ]]; then
      return 0
    fi

    sleep 5
  done

  echo "[phase3/verify] worker replicas not ready in time (expected=${desired})" >&2
  return 1
}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

if [[ "$DISABLE_OPERATOR_DURING_VERIFY" == "true" ]] && kubectl -n "$NAMESPACE" get deployment slurm-elastic-operator >/dev/null 2>&1; then
  operator_original_replicas=$(kubectl -n "$NAMESPACE" get deployment slurm-elastic-operator -o jsonpath='{.spec.replicas}')
  operator_original_replicas=${operator_original_replicas:-1}
  if [[ "$operator_original_replicas" -gt 0 ]]; then
    echo "[phase3/verify] scaling down slurm-elastic-operator from ${operator_original_replicas} to 0 during verification" >&2
    kubectl -n "$NAMESPACE" scale deployment/slurm-elastic-operator --replicas=0
    kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$VERIFY_TIMEOUT"
    operator_scaled_down="true"
  fi
fi

bash phase1/scripts/verify-phase1.sh

kubectl -n "$NAMESPACE" scale statefulset/slurm-worker --replicas="$WORKER_TARGET_REPLICAS"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$VERIFY_TIMEOUT"
wait_worker_replicas_ready "$WORKER_TARGET_REPLICAS"
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-0 --timeout="$VERIFY_TIMEOUT"
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-1 --timeout="$VERIFY_TIMEOUT"

worker_nodelist="slurm-worker-0,slurm-worker-1"

kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "
  getent hosts slurm-worker-0.slurm-worker.slurm.svc.cluster.local >/dev/null
  getent hosts slurm-worker-1.slurm-worker.slurm.svc.cluster.local >/dev/null
"

# Best-effort node state recovery. Keep silent to avoid noisy/unsupported-state errors
# on some Slurm versions (e.g. "Invalid node state specified").
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "
  for n in slurm-worker-0 slurm-worker-1; do
    scontrol update NodeName="\$n" State=UNDRAIN >/dev/null 2>&1 || true
    scontrol update NodeName="\$n" State=RESUME >/dev/null 2>&1 || true
  done
"

wait_slurm_nodes_ready

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
