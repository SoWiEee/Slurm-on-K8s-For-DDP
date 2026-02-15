#!/usr/bin/env bash
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-slurm-lab}"
NAMESPACE="${NAMESPACE:-slurm}"
CONTROLLER_POD="${CONTROLLER_POD:-slurm-controller-0}"
JOB_TIMEOUT_SECONDS="${JOB_TIMEOUT_SECONDS:-240}"
SLURM_RETRY_COUNT="${SLURM_RETRY_COUNT:-8}"
SLURM_RETRY_SLEEP_SECONDS="${SLURM_RETRY_SLEEP_SECONDS:-5}"

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
  start="$(date +%s)"

  while true; do
    queue_output="$(slurm_exec_retry "squeue -h -j ${job_id}" || true)"
    if [[ -z "${queue_output}" ]]; then
      return 0
    fi

    if (( $(date +%s) - start > JOB_TIMEOUT_SECONDS )); then
      log "timeout waiting for job ${job_id}"
      slurm_exec_retry "squeue -j ${job_id} || true" || true
      return 1
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

  if controller_exec "sacct -j ${job_id} --format=JobID,State,ExitCode --parsable2 >/dev/null 2>&1"; then
    slurm_exec_retry "sacct -j ${job_id} --format=JobID,State,ExitCode --parsable2 | tail -n +3" || true
    return
  fi

  log "sacct unavailable, fallback to scontrol show job ${job_id}"
  slurm_exec_retry "scontrol show job ${job_id} | sed -n '1,4p'" || true
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

  log "run MPI-like multi-node smoke"
  local mpi_job
  mpi_job="$(submit_job '/tmp/mpi-smoke.sbatch')"
  wait_job_done "${mpi_job}"
  print_job_status "${mpi_job}"

  log "run PyTorch/checkpoint step"
  local torch_job
  torch_job="$(submit_job '/tmp/pytorch-elastic.sbatch')"
  wait_job_done "${torch_job}"
  print_job_status "${torch_job}"

  log "phase3 verify completed"
}

main "$@"
