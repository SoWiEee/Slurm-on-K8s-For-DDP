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
      return 1
      ;;
  esac
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

  log "run MPI-like multi-node smoke"
  local mpi_job
  mpi_job="$(submit_job '/tmp/mpi-smoke.sbatch')"
  wait_job_done "${mpi_job}"
  print_job_status "${mpi_job}"
  assert_job_success "${mpi_job}" "phase3-mpi-smoke"

  log "run PyTorch/checkpoint step"
  local torch_job
  torch_job="$(submit_job '/tmp/pytorch-elastic.sbatch')"
  wait_job_done "${torch_job}"
  print_job_status "${torch_job}"
  assert_job_success "${torch_job}" "phase3-torch"

  log "phase3 verify completed"
}

main "$@"
