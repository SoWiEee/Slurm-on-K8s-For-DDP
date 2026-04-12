#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}

WORKER_STS=${WORKER_STS:-slurm-worker-cpu}
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

purge_stale_jobs() {
  # Cancel PENDING/RUNNING jobs, then force-clear COMPLETING jobs that are stuck
  # on pods that no longer exist.  Slurm won't advance a COMPLETING job unless
  # the node it ran on reconnects; setting the node to DOWN triggers an immediate
  # FAILED transition in slurmctld, unblocking the next slurmd registration cycle.
  log "Cancelling PENDING/RUNNING jobs (best-effort)..."
  kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
    'scancel -u root --state=PENDING --state=RUNNING 2>/dev/null || true'
  log "Clearing COMPLETING jobs stuck on missing pods..."
  local stuck_nodes
  stuck_nodes="$(kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
    "squeue -h -t CG -o '%N' 2>/dev/null | tr ',' '\n' | sort -u" 2>/dev/null || true)"
  for node in ${stuck_nodes}; do
    if ! kubectl -n "${NAMESPACE}" get pod "${node}" >/dev/null 2>&1; then
      log "  Forcing ${node} to DOWN (COMPLETING job, pod gone)..."
      kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
        "scontrol update NodeName=${node} State=DOWN Reason=cleanup || true" >/dev/null 2>&1 || true
    fi
  done
  # Wait up to 30s for COMPLETING jobs to drain
  local count=0
  for _ in $(seq 1 10); do
    count="$(kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
      'squeue -h -t CG -a 2>/dev/null | wc -l' || echo 0)"
    count="${count//[^0-9]/}"
    [[ "${count}" -eq 0 ]] && return 0
    log "  ${count} COMPLETING jobs still in queue, waiting..."
    sleep 3
  done
  log "Warning: ${count} COMPLETING jobs remain after timeout (proceeding anyway)"
}

mark_missing_nodes_down() {
  log "Marking missing worker nodes DOWN (best-effort)..."
  for n in $(seq 1 $((MAX_WORKERS - 1))); do
    if ! kubectl -n "${NAMESPACE}" get pod "${WORKER_STS}-${n}" >/dev/null 2>&1; then
      kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
        "scontrol update NodeName=${WORKER_STS}-${n} State=DOWN Reason='scaledown' || true" >/dev/null 2>&1 || true
    fi
  done
  # Resume any drained nodes whose pods ARE running (stale drain from previous test runs).
  # Without this, the operator won't call resume (no scale-up needed) and nodes stay DRAIN forever.
  log "Resuming stale-drained nodes whose pods are running (best-effort)..."
  for n in $(seq 0 $((MAX_WORKERS - 1))); do
    if kubectl -n "${NAMESPACE}" get pod "${WORKER_STS}-${n}" >/dev/null 2>&1; then
      kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
        "scontrol update NodeName=${WORKER_STS}-${n} State=resume || true" >/dev/null 2>&1 || true
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
  local worker_fqdn="${WORKER_STS}-${idx}.${WORKER_STS}.${NAMESPACE}.svc.cluster.local"
  local controller_fqdn="slurm-controller-0.slurm-controller.${NAMESPACE}.svc.cluster.local"
  local deadline ok0 okn
  deadline=$(( $(date +%s) + 120 ))

  log "Waiting for ${WORKER_STS}-${idx} pod to be Ready..."
  kubectl -n "${NAMESPACE}" wait --for=condition=Ready "pod/${WORKER_STS}-${idx}" --timeout=300s >/dev/null

  log "Waiting for DNS gates for ${WORKER_STS}-${idx}..."
  while true; do
    ok0=no
    okn=no
    if kubectl -n "${NAMESPACE}" exec "pod/${WORKER_STS}-0" -- sh -lc "getent hosts '${worker_fqdn}' >/dev/null" >/dev/null 2>&1; then ok0=yes; fi
    if kubectl -n "${NAMESPACE}" exec "pod/${WORKER_STS}-${idx}" -- sh -lc "getent hosts '${controller_fqdn}' >/dev/null" >/dev/null 2>&1; then okn=yes; fi
    if [[ "${ok0}" == yes && "${okn}" == yes ]]; then
      log "DNS OK: worker-0 -> ${worker_fqdn}, worker-${idx} -> ${controller_fqdn}"
      return 0
    fi
    (( $(date +%s) < deadline )) || return 1
    sleep 2
  done
}

wait_slurm_node_reason_ok() {
  # Wait until the node is no longer in a drained/down/not-responding state.
  # We check State (not Reason) because `scontrol update State=RESUME` clears
  # DRAIN but leaves the Reason field unchanged (it's cosmetic after resume).
  #
  # When the node is DRAIN but the pod is Ready, we proactively call
  # `scontrol update State=resume` on every poll — the operator only resumes
  # nodes it drained itself (tracked in _draining_nodes), so nodes set DOWN
  # by this test's setup must be resumed here.
  local nodename="$1" timeout_s="$2"
  local deadline state reason
  deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    state="$(kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
      "scontrol show node ${nodename} 2>/dev/null | awk -F'State=' 'NF>1{print \$2; exit}'" 2>/dev/null || true)"
    reason="$(kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
      "scontrol show node ${nodename} 2>/dev/null | awk -F'Reason=' 'NF>1{print \$2; exit}'" 2>/dev/null || true)"
    # Accept if State doesn't contain DRAIN or DOWN (node is schedulable)
    if [[ -n "${state}" ]] && ! echo "${state}" | grep -qiE "DRAIN|DOWN"; then
      log "${nodename} state ok: ${state} (reason: ${reason})"
      return 0
    fi
    # Proactively resume: operator won't do it for nodes this test set DOWN.
    if echo "${state}" | grep -qiE "DRAIN|DOWN"; then
      log "${nodename} state='${state}' — proactively calling scontrol resume..."
      kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
        "scontrol update NodeName=${nodename} State=resume || true" >/dev/null 2>&1 || true
    else
      log "${nodename} state='${state}' reason='${reason}' (waiting...)"
    fi
    (( $(date +%s) < deadline )) || return 1
    sleep 3
  done
}

# Wait for slurmd on a node to be heartbeating (State=IDLE without NOT_RESPONDING).
# sinfo shows "idle*" when NOT_RESPONDING; we need plain "idle" before submitting.
fix_node_addr() {
  # After a pod restart slurmctld retains the OLD pod IP in its address cache.
  # All subsequent outgoing RPCs (PING, TERMINATE_JOB, KILL_JOB) use the stale IP
  # and fail with "Connection timed out", causing the node to stay NOT_RESPONDING
  # and COMPLETING jobs to get stuck.
  # Fix: push the current pod IP into slurmctld with `scontrol update NodeAddr`.
  local nodename="$1"
  local pod_ip
  pod_ip="$(kubectl -n "${NAMESPACE}" get pod "${nodename}" -o jsonpath='{.status.podIP}' 2>/dev/null || true)"
  if [[ -n "${pod_ip}" ]]; then
    log "Refreshing slurmctld address cache for ${nodename}: NodeAddr=${pod_ip}"
    kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
      "scontrol update NodeName=${nodename} NodeAddr=${pod_ip} 2>&1 || true" >/dev/null 2>&1 || true
  fi
}

wait_slurm_node_responding() {
  # Wait until slurmd on the node is heartbeating (any state without trailing *).
  # sinfo shows "idle*", "mixed*", etc. when NOT_RESPONDING; without * = slurmd is up.
  #
  # Root causes addressed:
  # 1. Stale IP in slurmctld: on pod restart, slurmctld caches the old pod IP.
  #    Outgoing RPCs (PING, TERMINATE_JOB) target the old IP → timeout →
  #    NOT_RESPONDING and stuck COMPLETING.  Fix: update NodeAddr immediately.
  # 2. NP race at startup: CNI NP is applied a few seconds AFTER slurmd's first
  #    registration RPC.  The slurmctld back-ping ("registration agent") then
  #    fails leaving the node in idle*/NOT_RESPONDING.  After 30s we SIGHUP slurmd
  #    to force a fresh registration once the NP is applied.
  local nodename="$1" timeout_s="$2"
  local deadline state elapsed_since_star sighup_sent
  deadline=$(( $(date +%s) + timeout_s ))
  elapsed_since_star=0
  sighup_sent=0

  # Fix stale IP immediately before polling.
  fix_node_addr "${nodename}"

  while true; do
    state="$(kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
      "sinfo -N -h -n '${nodename}' -o '%T' 2>/dev/null" | tr -d '\r\n' || true)"
    # Any non-empty state without trailing * means slurmd is responding.
    if [[ -n "${state}" ]] && [[ "${state}" != *"*" ]]; then
      log "${nodename} slurmd responding: state=${state}"
      return 0
    fi
    log "${nodename} slurmd not yet responding (state='${state}'), waiting..."
    (( $(date +%s) < deadline )) || return 1
    # After 30s stuck in NOT_RESPONDING, SIGHUP slurmd to force re-registration.
    elapsed_since_star=$(( elapsed_since_star + 3 ))
    if [[ ${elapsed_since_star} -ge 30 ]] && [[ ${sighup_sent} -eq 0 ]]; then
      log "${nodename} stuck NOT_RESPONDING for 30s — sending SIGHUP to slurmd (force re-register)..."
      kubectl -n "${NAMESPACE}" exec "pod/${nodename}" -- bash -lc \
        'kill -HUP $(pgrep slurmd) 2>/dev/null && echo "SIGHUP sent" || echo "pgrep slurmd failed"' \
        2>/dev/null || true
      # Also refresh the IP in case it changed between polling cycles.
      fix_node_addr "${nodename}"
      sighup_sent=1
    fi
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
  # Retry up to 10s to allow NFS to flush the output file after job completion.
  local waited=0
  until kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc \
      "test -s '${SMOKE_DIR}/out-${jobid}.txt'" 2>/dev/null; do
    (( waited >= 10 )) && { echo "[e2e][ERROR] out-${jobid}.txt missing or empty after ${waited}s" >&2; exit 1; }
    sleep 2; waited=$(( waited + 2 ))
  done
  kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc \
    "echo '[e2e] out exists'; sed -n '1,220p' '${SMOKE_DIR}/out-${jobid}.txt'"

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
  # Wait for pods above MIN_WORKERS to fully terminate before touching Slurm node state.
  # A pod in Terminating still appears in `kubectl get pod`, so mark_missing_nodes_down
  # would incorrectly resume its Slurm node, making it appear schedulable.
  log "Waiting for excess worker pods to fully terminate..."
  for n in $(seq "${MIN_WORKERS}" $((MAX_WORKERS - 1))); do
    kubectl -n "${NAMESPACE}" wait pod "${WORKER_STS}-${n}" --for=delete --timeout=120s >/dev/null 2>&1 || true
  done

  log "Refresh slurmctld view (best-effort, no reconfigure — reconfigure blocks slurmctld DNS resolution)..."
  kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc 'sinfo -N -l || true' || true

  purge_stale_jobs
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

  # Submit with --hold so the job stays PENDING and never actually starts.
  # This prevents a COMPLETING state when we scancel later (since it was never RUNNING).
  # Held jobs are still counted as "pending" by the operator's slurmrestd query, so
  # the operator will scale up workers in response.
  trigger_jobid="$(exec_login "sbatch --hold '${SMOKE_DIR}/trigger.sbatch'" 2>/dev/null | awk '{print $NF}')"
  [[ "${trigger_jobid}" =~ ^[0-9]+$ ]] || die "failed to submit trigger job (got: '${trigger_jobid}')"
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

  # DNS + slurm reason + slurmd heartbeat gates for newly created workers
  for i in $(seq 1 $((TARGET_WORKERS - 1))); do
    wait_dns_gates_for_worker "${i}" || die "DNS gates failed for worker-${i}"
    wait_slurm_node_reason_ok "${WORKER_STS}-${i}" 120 || die "slurm reason not ok for worker-${i}"
    # Wait for slurmd heartbeat: sinfo shows "idle*" (NOT_RESPONDING) until first heartbeat.
    # Submitting before this causes srun to fail with "getaddrinfo: Name or service not known".
    wait_slurm_node_responding "${WORKER_STS}-${i}" 120 || die "slurmd not responding on worker-${i}"
  done

  log "Cancelling trigger job (it has served its purpose)..."
  kubectl -n "${NAMESPACE}" exec "pod/${login_pod}" -- bash -lc "scancel ${trigger_jobid} || true" >/dev/null 2>&1 || true

  # Wait for the trigger job to fully leave the queue (COMPLETING → gone).
  # If the trigger job was RUNNING when cancelled it enters COMPLETING state, which
  # keeps its nodes in completing*/allocated — blocking the real job from scheduling.
  log "Waiting for trigger job to fully clear queue..."
  local cg_wait=0
  while true; do
    local cg_count
    cg_count="$(kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc \
      "squeue -h -j ${trigger_jobid} -o '%T' 2>/dev/null | wc -l" 2>/dev/null | tr -d '[:space:]' || echo 1)"
    [[ "${cg_count//[^0-9]/}" -eq 0 ]] && break
    (( cg_wait < 60 )) || break
    log "  trigger job still in queue (${cg_count}), waiting..."
    sleep 3
    cg_wait=$(( cg_wait + 3 ))
  done
  # Also force-DOWN any COMPLETING nodes whose pod is gone (leftover from a previous run).
  purge_stale_jobs

  # Pause the operator so it cannot scale workers down while the real smoke job runs.
  # Without this, the operator sees 0 pending jobs (trigger cancelled) and starts a
  # scale-down, which drains the newly created worker pods and causes the job to get
  # stuck in COMPLETING (slurmd exits before the epilog can be acknowledged).
  log "Pausing elastic operator (prevent scale-down during smoke job)..."
  kubectl -n "${NAMESPACE}" scale deployment/slurm-elastic-operator --replicas=0 >/dev/null 2>&1 || true

  log "Submitting REAL multi-node smoke job from login..."
  local jobid
  jobid="$(submit_sbatch_jobid "${login_pod}" "${SMOKE_DIR}/phase3-smoke.sbatch")"
  [[ -n "${jobid}" ]] || die "failed to submit smoke job"
  log "jobid=${jobid}"

  # NOTE: do NOT call `scontrol reconfigure` here.
  # Reconfigure causes slurmctld to resolve every static node FQDN (including
  # scaled-to-zero replicas whose DNS does not exist), blocking the daemon for
  # 30-60 s.  During that window srun cannot dispatch the step and the job fails.
  log "Waiting for job to finish..."
  wait_job_finish "${login_pod}" "${jobid}" 300 || { \
    kubectl -n "${NAMESPACE}" scale deployment/slurm-elastic-operator --replicas=1 >/dev/null 2>&1 || true; \
    die "job ${jobid} did not finish in time"; \
  }

  # Restore the operator now that the smoke job has finished.
  log "Restoring elastic operator..."
  kubectl -n "${NAMESPACE}" scale deployment/slurm-elastic-operator --replicas=1 >/dev/null 2>&1 || true

  verify_output_hosts "${login_pod}" "${jobid}" "${SMOKE_NODES}"

  echo "Phase 3 E2E verification passed: sbatch ran a ${SMOKE_NODES}-node job and output is in shared (${SMOKE_DIR}) path."
}

main "$@"
