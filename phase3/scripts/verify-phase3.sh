#!/usr/bin/env bash
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-slurm-lab}"
NAMESPACE="${NAMESPACE:-slurm}"
CONTROLLER_POD="${CONTROLLER_POD:-slurm-controller-0}"
JOB_TIMEOUT_SECONDS="${JOB_TIMEOUT_SECONDS:-240}"

log() {
  printf '[phase3 verify] %s\n' "$*"
}

controller_exec() {
  kubectl -n "${NAMESPACE}" exec "pod/${CONTROLLER_POD}" -- bash -lc "$*"
}

wait_job_done() {
  local job_id="$1"
  local start
  start="$(date +%s)"

  while true; do
    if ! controller_exec "squeue -h -j ${job_id}" | rg -q .; then
      return 0
    fi

    if (( $(date +%s) - start > JOB_TIMEOUT_SECONDS )); then
      log "timeout waiting for job ${job_id}"
      controller_exec "squeue -j ${job_id} || true"
      return 1
    fi

    sleep 4
  done
}

main() {
  kubectl config use-context "${KUBE_CONTEXT}" >/dev/null

  log "checking ConfigMap + shared mount"
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs >/dev/null
  controller_exec 'test -d /shared'
  log "injecting phase3 job scripts to controller pod"
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs -o jsonpath='{.data.shared-storage\.sbatch}' | \
    kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc 'cat > /tmp/shared-storage.sbatch'
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs -o jsonpath='{.data.mpi-smoke\.sbatch}' | \
    kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc 'cat > /tmp/mpi-smoke.sbatch'
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs -o jsonpath='{.data.pytorch-elastic\.sbatch}' | \
    kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc 'cat > /tmp/pytorch-elastic.sbatch'

  log "run shared storage smoke"
  local shared_job
  shared_job="$(controller_exec 'sbatch /tmp/shared-storage.sbatch' | awk '{print $4}')"
  wait_job_done "${shared_job}"
  controller_exec "ls -1 /shared/phase3 | tail -n 3"

  log "run MPI-like multi-node smoke"
  local mpi_job
  mpi_job="$(controller_exec 'sbatch /tmp/mpi-smoke.sbatch' | awk '{print $4}')"
  wait_job_done "${mpi_job}"
  controller_exec "sacct -j ${mpi_job} --format=JobID,State,NodeList --parsable2 | tail -n +3"

  log "run PyTorch/checkpoint step"
  local torch_job
  torch_job="$(controller_exec 'sbatch /tmp/pytorch-elastic.sbatch' | awk '{print $4}')"
  wait_job_done "${torch_job}"
  controller_exec "sacct -j ${torch_job} --format=JobID,State,ExitCode --parsable2 | tail -n +3"

  log "phase3 verify completed"
}

main "$@"
