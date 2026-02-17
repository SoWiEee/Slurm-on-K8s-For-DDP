#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}

WORKER_STS=${WORKER_STS:-slurm-worker}
CONTROLLER_POD=${CONTROLLER_POD:-slurm-controller-0}
LOGIN_LABEL_SELECTOR=${LOGIN_LABEL_SELECTOR:-app=slurm-login}

PARTITION=${PARTITION:-debug}
SMOKE_DIR=${SMOKE_DIR:-/shared/phase3-smoke}

# Scaling knobs
MIN_WORKERS=${MIN_WORKERS:-1}
TARGET_WORKERS=${TARGET_WORKERS:-2}
MAX_WORKERS=${MAX_WORKERS:-4}

# How many nodes the smoke job should require
SMOKE_NODES=${SMOKE_NODES:-$(( TARGET_WORKERS < MAX_WORKERS ? TARGET_WORKERS : MAX_WORKERS ))}

log() { echo "[e2e] $*" >&2; }

die() { echo "[e2e][ERROR] $*" >&2; exit 1; }

require_context() {
  log "Using context: ${KUBE_CONTEXT}"
  kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$" || die "kubectl context ${KUBE_CONTEXT} not found"
  kubectl config use-context "${KUBE_CONTEXT}" >/dev/null
}

get_login_pod() {
  kubectl -n "${NAMESPACE}" get pod -l "${LOGIN_LABEL_SELECTOR}" -o jsonpath='{.items[0].metadata.name}'
}

exec_login() {
  local cmd="$1"
  local pod
  pod="$(get_login_pod)"
  kubectl -n "${NAMESPACE}" exec "pod/${pod}" -- bash -lc "${cmd}"
}

wait_rollouts() {
  log "Waiting for login/controller/worker to be ready..."
  kubectl -n "${NAMESPACE}" rollout status deployment/slurm-login --timeout="${ROLLOUT_TIMEOUT}" >/dev/null
  kubectl -n "${NAMESPACE}" rollout status statefulset/${WORKER_STS} --timeout="${ROLLOUT_TIMEOUT}" >/dev/null
  kubectl -n "${NAMESPACE}" rollout status statefulset/slurm-controller --timeout="${ROLLOUT_TIMEOUT}" >/dev/null
}

mark_missing_nodes_down() {
  log "Marking missing worker nodes DOWN (best-effort)..."
  for n in $(seq 1 $((MAX_WORKERS - 1))); do
    if ! kubectl -n "${NAMESPACE}" get pod "slurm-worker-${n}" >/dev/null 2>&1; then
      kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
        "scontrol update NodeName=slurm-worker-${n} State=DOWN Reason='scaledown' || true" >/dev/null 2>&1 || true
    fi
  done
  kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc 'sinfo -N -l || true' || true
}

write_smoke_sbatch_to_shared() {
  local login_pod
  login_pod="$(get_login_pod)"
  log "login_pod=${login_pod}"
  log "Writing smoke sbatch script into ${SMOKE_DIR} (no host-side expansion of SLURM env)..."

  kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc "mkdir -p '${SMOKE_DIR}'"

  # IMPORTANT: this kubectl exec command string is double-quoted, so we must escape any $ or $(...)
  # that should remain literal inside the sbatch script at runtime.
  kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc "cat > '${SMOKE_DIR}/phase3-smoke.sbatch' <<'E2E_SBATCH'
#!/usr/bin/env bash
#SBATCH -J phase3-smoke
#SBATCH -p __PARTITION__
#SBATCH -N __NODES__
#SBATCH -o ${SMOKE_DIR}/out-%j.txt
#SBATCH -e ${SMOKE_DIR}/err-%j.txt
set -euo pipefail

echo \"jobid=\${SLURM_JOB_ID}\"
echo \"nodelist=\${SLURM_NODELIST}\"
echo \"[\$(date -u)] starting on \$(hostname -s)\"

# One task per node; prefix output for stable parsing.
srun -N __NODES__ -n __NODES__ hostname -s | sed 's/^/hello from /'
E2E_SBATCH
sed -i -e 's/__PARTITION__/${PARTITION}/g' -e 's/__NODES__/${SMOKE_NODES}/g' '${SMOKE_DIR}/phase3-smoke.sbatch'
chmod +x '${SMOKE_DIR}/phase3-smoke.sbatch'"
}

submit_sbatch_jobid() {
  local login_pod="$1" sbatch_path="$2"
  kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc \
    "sbatch '${sbatch_path}' | awk '{print \$4}'" | tr -d '\r\n'
}

wait_job_state() {
  local login_pod="$1" jobid="$2" want="$3" timeout_s="$4"
  local deadline st
  deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    st="$(kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc "squeue -h -j ${jobid} -o %T 2>/dev/null || true" | tr -d '\r')"
    if [[ -n "${st}" ]]; then
      log "job ${jobid} state=${st}"
      if [[ "${st}" == "${want}" ]]; then
        return 0
      fi
    else
      log "job ${jobid} state=gone"
    fi
    (( $(date +%s) < deadline )) || return 1
    sleep 3
  done
}

wait_workers_scaled_ready() {
  local want="$1" timeout_s="$2"
  local deadline replicas ready
  deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    replicas="$(kubectl -n "${NAMESPACE}" get sts "${WORKER_STS}" -o jsonpath='{.spec.replicas}')"
    ready="$(kubectl -n "${NAMESPACE}" get sts "${WORKER_STS}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
    log "worker_replicas=${replicas} ready=${ready}"
    if (( replicas >= want && ready >= want )); then
      return 0
    fi
    (( $(date +%s) < deadline )) || return 1
    sleep 5
  done
}

wait_dns_gates_for_worker() {
  local idx="$1"
  local worker_fqdn="slurm-worker-${idx}.slurm-worker.${NAMESPACE}.svc.cluster.local"
  local controller_fqdn="slurm-controller-0.slurm-controller.${NAMESPACE}.svc.cluster.local"
  local deadline ok0 okn
  deadline=$(( $(date +%s) + 120 ))

  log "Waiting for slurm-worker-${idx} pod to be Ready..."
  kubectl -n "${NAMESPACE}" wait --for=condition=Ready "pod/slurm-worker-${idx}" --timeout=300s >/dev/null

  log "Waiting for DNS gates for slurm-worker-${idx}..."
  while true; do
    ok0=no
    okn=no
    if kubectl -n "${NAMESPACE}" exec pod/slurm-worker-0 -- sh -lc "getent hosts '${worker_fqdn}' >/dev/null" >/dev/null 2>&1; then ok0=yes; fi
    if kubectl -n "${NAMESPACE}" exec "pod/slurm-worker-${idx}" -- sh -lc "getent hosts '${controller_fqdn}' >/dev/null" >/dev/null 2>&1; then okn=yes; fi
    if [[ "${ok0}" == yes && "${okn}" == yes ]]; then
      log "DNS OK: worker-0 -> ${worker_fqdn}, worker-${idx} -> ${controller_fqdn}"
      return 0
    fi
    (( $(date +%s) < deadline )) || return 1
    sleep 2
  done
}

wait_slurm_node_reason_ok() {
  local nodename="$1" timeout_s="$2"
  local deadline reason
  deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    reason="$(kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
      "scontrol show node ${nodename} | awk -F'Reason=' 'NF>1{print \$2; exit}'" 2>/dev/null || true)"
    if [[ -z "${reason}" ]] || [[ "${reason}" == *"none"* ]]; then
      log "${nodename} reason ok: none"
      return 0
    fi
    log "${nodename} reason='${reason}' (waiting...)"
    (( $(date +%s) < deadline )) || return 1
    sleep 3
  done
}

wait_job_finish() {
  local login_pod="$1" jobid="$2" timeout_s="$3"
  local deadline st
  deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    st="$(kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc "squeue -h -j ${jobid} -o %T 2>/dev/null || true" | tr -d '\r')"
    if [[ -z "${st}" ]]; then
      log "job ${jobid} not in queue anymore (finished)."
      return 0
    fi
    log "job ${jobid} state=${st}"
    (( $(date +%s) < deadline )) || return 1
    sleep 5
  done
}

verify_output_hosts() {
  local login_pod="$1" jobid="$2" need="$3"

  log "Verifying output exists on shared path: ${SMOKE_DIR}/out-${jobid}.txt"
  kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc \
    "test -s '${SMOKE_DIR}/out-${jobid}.txt' && echo '[e2e] out exists' && sed -n '1,220p' '${SMOKE_DIR}/out-${jobid}.txt'"

  log "Checking output contains >=${need} distinct hostnames (multi-node evidence)..."
  local distinct
  distinct="$(kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc \
    "grep -Eo 'hello from [^ ]+' '${SMOKE_DIR}/out-${jobid}.txt' | awk '{print \$3}' | sort -u | wc -l")"
  log "distinct_hosts=${distinct}"

  if (( distinct < need )); then
    echo "[e2e][ERROR] Expected output from >=${need} hosts, got ${distinct}. Dumping out/err:" >&2
    kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc \
      "sed -n '1,260p' '${SMOKE_DIR}/out-${jobid}.txt' || true; sed -n '1,260p' '${SMOKE_DIR}/err-${jobid}.txt' || true" || true
    exit 1
  fi
}

main() {
  require_context
  wait_rollouts

  log "Checking sbatch exists on login..."
  exec_login 'command -v sbatch >/dev/null && sbatch --version' >/dev/null

  log "Forcing workers to MIN_WORKERS=${MIN_WORKERS} (baseline)..."
  kubectl -n "${NAMESPACE}" scale statefulset/${WORKER_STS} --replicas="${MIN_WORKERS}" >/dev/null
  kubectl -n "${NAMESPACE}" rollout status statefulset/${WORKER_STS} --timeout="${ROLLOUT_TIMEOUT}" >/dev/null

  log "Refresh slurmctld view (best-effort)..."
  kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc 'scontrol reconfigure || true; sinfo -N -l || true' || true

  mark_missing_nodes_down
  write_smoke_sbatch_to_shared

  local login_pod trigger_jobid
  login_pod="$(get_login_pod)"

  log "Submitting trigger job that requires ${SMOKE_NODES} nodes (expected PENDING with MIN_WORKERS=${MIN_WORKERS})..."
  exec_login "cat > '${SMOKE_DIR}/trigger.sbatch' <<'EOF_TRIG'
#!/usr/bin/env bash
#SBATCH -J phase3-trigger
#SBATCH -p ${PARTITION}
#SBATCH -N ${SMOKE_NODES}
#SBATCH -o ${SMOKE_DIR}/trigger-out-%j.txt
#SBATCH -e ${SMOKE_DIR}/trigger-err-%j.txt
echo trigger-start \$(date)
sleep 60
EOF_TRIG" >/dev/null

  trigger_jobid="$(submit_sbatch_jobid "${login_pod}" "${SMOKE_DIR}/trigger.sbatch")"
  [[ -n "${trigger_jobid}" ]] || die "failed to submit trigger job"
  log "trigger_jobid=${trigger_jobid}"

  log "Waiting for trigger job to reach PENDING (operator scale-up trigger)..."
  wait_job_state "${login_pod}" "${trigger_jobid}" "PENDING" 120 || die "trigger job did not reach PENDING"

  log "Waiting for operator to scale workers to TARGET_WORKERS=${TARGET_WORKERS} (fallback to manual scale)..."
  if ! wait_workers_scaled_ready "${TARGET_WORKERS}" 120; then
    log "Operator did not scale in time; doing manual scale to ${TARGET_WORKERS}"
    kubectl -n "${NAMESPACE}" scale statefulset/${WORKER_STS} --replicas="${TARGET_WORKERS}" >/dev/null
    wait_workers_scaled_ready "${TARGET_WORKERS}" 300 || die "workers not ready after manual scale"
  fi
  log "Workers scaled and ready."

  # DNS + slurm reason gates for newly created workers
  for i in $(seq 1 $((TARGET_WORKERS - 1))); do
    wait_dns_gates_for_worker "${i}" || die "DNS gates failed for worker-${i}"
    wait_slurm_node_reason_ok "slurm-worker-${i}" 120 || die "slurm reason not ok for worker-${i}"
  done

  log "Cancelling trigger job (it has served its purpose)..."
  kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc "scancel ${trigger_jobid} || true" >/dev/null 2>&1 || true

  log "Submitting REAL multi-node smoke job from login..."
  local jobid
  jobid="$(submit_sbatch_jobid "${login_pod}" "${SMOKE_DIR}/phase3-smoke.sbatch")"
  [[ -n "${jobid}" ]] || die "failed to submit smoke job"
  log "jobid=${jobid}"

  log "Refreshing slurmctld view (best-effort)..."
  kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc 'scontrol reconfigure || true; sinfo -N -l || true' || true

  log "Waiting for job to finish..."
  wait_job_finish "${login_pod}" "${jobid}" 300 || die "job ${jobid} did not finish in time"

  verify_output_hosts "${login_pod}" "${jobid}" "${SMOKE_NODES}"

  echo "Phase 3 E2E verification passed: sbatch ran a ${SMOKE_NODES}-node job and output is in shared (${SMOKE_DIR}) path."
}

main "$@"
