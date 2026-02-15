#!/usr/bin/env bash
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-slurm-lab}"
NAMESPACE="${NAMESPACE:-slurm}"
CONTROLLER_POD="${CONTROLLER_POD:-slurm-controller-0}"
JOB_TIMEOUT_SECONDS="${JOB_TIMEOUT_SECONDS:-240}"
SLURM_RETRY_COUNT="${SLURM_RETRY_COUNT:-8}"
SLURM_RETRY_SLEEP_SECONDS="${SLURM_RETRY_SLEEP_SECONDS:-5}"
JOB_COMPLETING_GRACE_SECONDS="${JOB_COMPLETING_GRACE_SECONDS:-300}"
COMPLETING_LOG_INTERVAL_SECONDS="${COMPLETING_LOG_INTERVAL_SECONDS:-30}"
VERIFY_MIN_WORKER_REPLICAS="${VERIFY_MIN_WORKER_REPLICAS:-2}"
SLURM_NODE_READY_TIMEOUT_SECONDS="${SLURM_NODE_READY_TIMEOUT_SECONDS:-240}"

log() {
  printf '[phase3 verify] %s\n' "$*"
}

controller_exec() {
  kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc "$*"
}

is_retryable_slurm_error() {
  grep -Eq 'Socket timed out|Unable to contact slurm controller|slurm_load_jobs error|Connection refused|slurmctld.*down'
}

slurm_exec_retry() {
  local command="$1"
  local attempt=1
  local output

  while (( attempt <= SLURM_RETRY_COUNT )); do
    if output="$(controller_exec "${command}" 2>&1)"; then
      printf '%s\n' "${output}"
      return 0
    fi

    if printf '%s\n' "${output}" | is_retryable_slurm_error; then
      log "slurm command retry (${attempt}/${SLURM_RETRY_COUNT}): ${command}"
      log "reason: $(printf '%s' "${output}" | tr '\n' ' ' | cut -c1-180)"
      sleep "${SLURM_RETRY_SLEEP_SECONDS}"
      attempt=$((attempt + 1))
      continue
    fi

    printf '%s\n' "${output}" >&2
    return 1
  done

  printf '%s\n' "${output}" >&2
  return 1
}

wait_job_done() {
  local job_id="$1"
  local start
  local queue_output
  local states_output
  local hard_deadline
  local completing_deadline
  local last_completing_log=0
  local now

  start="$(date +%s)"
  hard_deadline=$((start + JOB_TIMEOUT_SECONDS))
  completing_deadline=$((hard_deadline + JOB_COMPLETING_GRACE_SECONDS))

  while true; do
    queue_output="$(slurm_exec_retry "squeue -h -j ${job_id}" || true)"
    if [[ -z "${queue_output}" ]]; then
      return 0
    fi

    states_output="$(slurm_exec_retry "squeue -h -j ${job_id} -o '%T'" || true)"
    now="$(date +%s)"

    if (( now > hard_deadline )); then
      if [[ -n "${states_output}" ]] && printf '%s\n' "${states_output}" | grep -Evq 'COMPLETING|COMPLETED'; then
        log "timeout waiting for job ${job_id}"
        slurm_exec_retry "squeue -j ${job_id} || true" || true
        return 1
      fi

      if (( now > completing_deadline )); then
        log "timeout waiting for job ${job_id} (including completing grace ${JOB_COMPLETING_GRACE_SECONDS}s)"
        slurm_exec_retry "squeue -j ${job_id} || true" || true
        return 1
      fi

      if (( now - last_completing_log >= COMPLETING_LOG_INTERVAL_SECONDS )); then
        log "job ${job_id} is completing; waiting extra grace window (${JOB_COMPLETING_GRACE_SECONDS}s)"
        last_completing_log="${now}"
      fi
    fi

    sleep 4
  done
}


submit_job() {
  local script_path="$1"
  local out
  out="$(slurm_exec_retry "sbatch ${script_path}")"
  printf '%s\n' "${out}" | awk '{print $4}'
}

print_job_status() {
  local job_id="$1"

  log "job ${job_id} status (scontrol)"
  slurm_exec_retry "scontrol show job ${job_id} | sed -n '1,8p'" || true

  if [[ "${ENABLE_SACCT_STATUS:-false}" == "true" ]]; then
    log "job ${job_id} status (sacct enabled by flag)"
    slurm_exec_retry "sacct -j ${job_id} --format=JobID,State,ExitCode --parsable2 | tail -n +3" || true
  fi
}


get_job_state_exit() {
  local job_id="$1"
  local line
  line="$(slurm_exec_retry "scontrol show job ${job_id} -o")"
  printf '%s\n' "${line}" | sed -n 's/.*JobState=\([^ ]*\).*ExitCode=\([^ ]*\).*/\1 \2/p'
}


assert_job_success() {
  local job_id="$1"
  local name="$2"
  local state_exit
  local state
  local exit_code

  state_exit="$(get_job_state_exit "${job_id}")"
  state="$(printf '%s' "${state_exit}" | awk '{print $1}')"
  exit_code="$(printf '%s' "${state_exit}" | awk '{print $2}')"

  case "${state}" in
    COMPLETED)
      if [[ "${exit_code}" != "0:0" ]]; then
        log "job ${job_id} (${name}) exit code is ${exit_code} (expected 0:0)"
        controller_exec "test -f /root/slurm-${job_id}.out && tail -n 80 /root/slurm-${job_id}.out || true"
        return 1
      fi
      return 0
      ;;
    *)
      log "job ${job_id} (${name}) failed with state=${state} exit=${exit_code}"
      controller_exec "test -f /root/slurm-${job_id}.out && tail -n 120 /root/slurm-${job_id}.out || true"
      slurm_exec_retry "sinfo -R || true" || true
      slurm_exec_retry "scontrol show nodes | sed -n '1,120p'" || true
      return 1
      ;;
  esac
}


ensure_worker_capacity() {
  if (( VERIFY_MIN_WORKER_REPLICAS <= 0 )); then
    return
  fi

  log "ensuring worker replicas >= ${VERIFY_MIN_WORKER_REPLICAS} for multi-node checks"
  kubectl -n "${NAMESPACE}" scale statefulset/slurm-worker --replicas="${VERIFY_MIN_WORKER_REPLICAS}" >/dev/null
  kubectl -n "${NAMESPACE}" rollout status statefulset/slurm-worker --timeout="${SLURM_NODE_READY_TIMEOUT_SECONDS}s"
}

wait_slurm_nodes_ready() {
  local required_nodes="$1"
  local start
  local lines
  local ready_count

  start="$(date +%s)"
  while true; do
    lines="$(slurm_exec_retry "sinfo -h -N -p debug -o '%T'" || true)"
    ready_count="$(printf '%s\n' "${lines}" | grep -Eic 'idle|mix|allocated|alloc')"

    if (( ready_count >= required_nodes )); then
      log "slurm nodes ready: ${ready_count}/${required_nodes}"
      return 0
    fi

    if (( $(date +%s) - start > SLURM_NODE_READY_TIMEOUT_SECONDS )); then
      log "timeout waiting slurm nodes ready: need ${required_nodes}, got ${ready_count}"
      slurm_exec_retry "sinfo -R || true" || true
      slurm_exec_retry "scontrol show nodes | sed -n '1,120p'" || true
      return 1
    fi

    sleep 4
  done
}


verify_worker_daemons() {
  local pods_raw
  local pod_count
  local pod

  if (( VERIFY_MIN_WORKER_REPLICAS <= 0 )); then
    return
  fi

  pods_raw="$(kubectl -n "${NAMESPACE}" get pods -l app=slurm-worker --field-selector=status.phase=Running -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' | sort)"
  pod_count="$(printf '%s\n' "${pods_raw}" | sed '/^$/d' | wc -l | tr -d ' ')"

  if (( pod_count < VERIFY_MIN_WORKER_REPLICAS )); then
    log "running worker pods are insufficient: ${pod_count}/${VERIFY_MIN_WORKER_REPLICAS}"
    kubectl -n "${NAMESPACE}" get pods -l app=slurm-worker -o wide || true
    return 1
  fi

  while read -r pod; do
    [[ -z "${pod}" ]] && continue
    log "checking daemon health on ${pod}"

    if ! kubectl -n "${NAMESPACE}" exec "pod/${pod}" -- bash -lc 'pgrep -x munged >/dev/null && pgrep -x slurmd >/dev/null'; then
      log "${pod} daemon health check failed"
      kubectl -n "${NAMESPACE}" get pod "${pod}" -o wide || true
      kubectl -n "${NAMESPACE}" logs "pod/${pod}" --tail=120 || true
      return 1
    fi
  done < <(printf '%s\n' "${pods_raw}" | sed '/^$/d' | head -n "${VERIFY_MIN_WORKER_REPLICAS}")
}


recover_unhealthy_nodes() {
  local lines
  local node
  local state

  lines="$(slurm_exec_retry "sinfo -h -N -p debug -o '%N %T'" || true)"
  while read -r node state; do
    if [[ -z "${node:-}" ]]; then
      continue
    fi
    if printf '%s' "${state}" | grep -Eiq 'down|drain|not_responding|fail'; then
      log "attempting RESUME for unhealthy node ${node} state=${state}"
      slurm_exec_retry "scontrol update NodeName=${node} State=RESUME" || true
    fi
  done <<< "${lines}"
}

assert_mpi_output() {
  local job_id="$1"
  local hosts
  local unique

  hosts="$(controller_exec "test -f /root/slurm-${job_id}.out && awk -F= '/^rank-host=/{print \$2}' /root/slurm-${job_id}.out || true")"
  unique="$(printf '%s\n' "${hosts}" | sed '/^$/d' | sort -u | wc -l | tr -d ' ')"

  if [[ -z "${unique}" || "${unique}" -lt 2 ]]; then
    log "mpi output validation failed for job ${job_id}: unique_hosts=${unique:-0}"
    controller_exec "test -f /root/slurm-${job_id}.out && tail -n 120 /root/slurm-${job_id}.out || true"
    return 1
  fi

  log "mpi output validation passed: unique_hosts=${unique}"
}


main() {
  kubectl config use-context "${KUBE_CONTEXT}" >/dev/null

  log "checking ConfigMap + shared mount"
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs >/dev/null
  controller_exec 'test -d /shared'
  slurm_exec_retry 'sinfo -h' >/dev/null

  log "injecting phase3 job scripts to controller pod"
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs -o jsonpath='{.data.shared-storage\.sbatch}' | \
    kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc 'cat > /tmp/shared-storage.sbatch'
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs -o jsonpath='{.data.mpi-smoke\.sbatch}' | \
    kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc 'cat > /tmp/mpi-smoke.sbatch'
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs -o jsonpath='{.data.pytorch-elastic\.sbatch}' | \
    kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc 'cat > /tmp/pytorch-elastic.sbatch'

  log "run shared storage smoke"
  local shared_job
  shared_job="$(submit_job '/tmp/shared-storage.sbatch')"
  wait_job_done "${shared_job}"
  controller_exec "ls -1 /shared/phase3 | tail -n 3"
  print_job_status "${shared_job}"
  assert_job_success "${shared_job}" "phase3-shared"

  ensure_worker_capacity
  verify_worker_daemons
  recover_unhealthy_nodes
  wait_slurm_nodes_ready 2

  log "run MPI-like multi-node smoke"
  local mpi_job
  mpi_job="$(submit_job '/tmp/mpi-smoke.sbatch')"
  wait_job_done "${mpi_job}"
  print_job_status "${mpi_job}"
  assert_job_success "${mpi_job}" "phase3-mpi-smoke"
  assert_mpi_output "${mpi_job}"

  log "run PyTorch/checkpoint step"
  local torch_job
  torch_job="$(submit_job '/tmp/pytorch-elastic.sbatch')"
  wait_job_done "${torch_job}"
  print_job_status "${torch_job}"
  assert_job_success "${torch_job}" "phase3-torch"

  log "phase3 verify completed"
}

main "$@"
