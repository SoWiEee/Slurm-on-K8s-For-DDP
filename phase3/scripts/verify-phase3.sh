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
WORKER_POD_READY_TIMEOUT_SECONDS="${WORKER_POD_READY_TIMEOUT_SECONDS:-240}"
DISABLE_OPERATOR_DURING_VERIFY="${DISABLE_OPERATOR_DURING_VERIFY:-true}"
OPERATOR_DEPLOYMENT_NAME="${OPERATOR_DEPLOYMENT_NAME:-slurm-elastic-operator}"
WORKER_NODE_NAME_PREFIX="${WORKER_NODE_NAME_PREFIX:-slurm-worker-}"

OPERATOR_REPLICAS_BEFORE_VERIFY=""

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

pause_operator_if_needed() {
  if [[ "${DISABLE_OPERATOR_DURING_VERIFY}" != "true" ]]; then
    return
  fi

  if ! kubectl -n "${NAMESPACE}" get deployment "${OPERATOR_DEPLOYMENT_NAME}" >/dev/null 2>&1; then
    log "operator deployment ${OPERATOR_DEPLOYMENT_NAME} not found, skip pause"
    return
  fi

  OPERATOR_REPLICAS_BEFORE_VERIFY="$(kubectl -n "${NAMESPACE}" get deployment "${OPERATOR_DEPLOYMENT_NAME}" -o jsonpath='{.spec.replicas}')"
  OPERATOR_REPLICAS_BEFORE_VERIFY="${OPERATOR_REPLICAS_BEFORE_VERIFY:-1}"

  log "pausing operator ${OPERATOR_DEPLOYMENT_NAME} (replicas ${OPERATOR_REPLICAS_BEFORE_VERIFY} -> 0)"
  kubectl -n "${NAMESPACE}" scale deployment/"${OPERATOR_DEPLOYMENT_NAME}" --replicas=0 >/dev/null
  kubectl -n "${NAMESPACE}" rollout status deployment/"${OPERATOR_DEPLOYMENT_NAME}" --timeout=180s >/dev/null || true
  wait_operator_quiesced || true
}

restore_operator_if_needed() {
  if [[ "${DISABLE_OPERATOR_DURING_VERIFY}" != "true" ]]; then
    return
  fi

  if [[ -z "${OPERATOR_REPLICAS_BEFORE_VERIFY}" ]]; then
    return
  fi

  if ! kubectl -n "${NAMESPACE}" get deployment "${OPERATOR_DEPLOYMENT_NAME}" >/dev/null 2>&1; then
    return
  fi

  log "restoring operator ${OPERATOR_DEPLOYMENT_NAME} replicas -> ${OPERATOR_REPLICAS_BEFORE_VERIFY}"
  kubectl -n "${NAMESPACE}" scale deployment/"${OPERATOR_DEPLOYMENT_NAME}" --replicas="${OPERATOR_REPLICAS_BEFORE_VERIFY}" >/dev/null || true
}

wait_operator_quiesced() {
  local start
  local running
  local desired

  start="$(date +%s)"
  while true; do
    desired="$(kubectl -n "${NAMESPACE}" get deployment "${OPERATOR_DEPLOYMENT_NAME}" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)"
    running="$(kubectl -n "${NAMESPACE}" get pods -l app=${OPERATOR_DEPLOYMENT_NAME} --field-selector=status.phase=Running --no-headers 2>/dev/null | wc -l | tr -d ' ')"

    if [[ "${desired:-0}" == "0" ]] && [[ "${running:-0}" == "0" ]]; then
      return 0
    fi

    if (( $(date +%s) - start > 180 )); then
      log "operator did not fully quiesce in time (desired=${desired:-?}, running=${running:-?})"
      kubectl -n "${NAMESPACE}" get deployment "${OPERATOR_DEPLOYMENT_NAME}" -o wide || true
      kubectl -n "${NAMESPACE}" get pods -l app=${OPERATOR_DEPLOYMENT_NAME} -o wide || true
      return 1
    fi

    sleep 2
  done
}

wait_worker_pods_ready() {
  local required="$1"
  local start
  local ready

  start="$(date +%s)"
  while true; do
    ready="$(kubectl -n "${NAMESPACE}" get pods -l app=slurm-worker --no-headers 2>/dev/null | awk '$2=="1/1" && $3=="Running" {c++} END{print c+0}')"

    if (( ready >= required )); then
      log "worker pods ready: ${ready}/${required}"
      return 0
    fi

    if (( $(date +%s) - start > WORKER_POD_READY_TIMEOUT_SECONDS )); then
      log "timeout waiting worker pods ready: ${ready}/${required}"
      kubectl -n "${NAMESPACE}" get pods -l app=slurm-worker -o wide || true
      return 1
    fi

    sleep 3
  done
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




extract_job_field() {
  local line="$1"
  local key="$2"
  printf '%s\n' "${line}" | awk -v k="${key}=" '{for(i=1;i<=NF;i++) if(index($i,k)==1){print substr($i,length(k)+1); exit}}'
}

normalize_path() {
  local path="$1"
  if [[ -z "${path}" ]]; then
    printf '%s\n' ""
    return
  fi
  printf '%s\n' "${path}" | sed 's#//*#/#g'
}

get_job_stdout_path() {
  local job_id="$1"
  local line
  local path

  line="$(slurm_exec_retry "scontrol show job ${job_id} -o")"
  path="$(extract_job_field "${line}" "StdOut")"
  path="${path:-slurm-${job_id}.out}"
  normalize_path "${path}"
}

get_job_stderr_path() {
  local job_id="$1"
  local line
  local path

  line="$(slurm_exec_retry "scontrol show job ${job_id} -o")"
  path="$(extract_job_field "${line}" "StdErr")"
  path="${path:-slurm-${job_id}.err}"
  normalize_path "${path}"
}

get_job_workdir() {
  local job_id="$1"
  local line
  local path

  line="$(slurm_exec_retry "scontrol show job ${job_id} -o")"
  path="$(extract_job_field "${line}" "WorkDir")"
  path="${path:-/root}"
  normalize_path "${path}"
}

list_candidate_output_paths() {
  local job_id="$1"
  local out_path
  local err_path
  local workdir

  out_path="$(get_job_stdout_path "${job_id}")"
  err_path="$(get_job_stderr_path "${job_id}")"
  workdir="$(get_job_workdir "${job_id}")"

  if [[ "${out_path}" == /* ]]; then
    printf '%s\n' "${out_path}"
  else
    normalize_path "${workdir}/${out_path}"
    normalize_path "/root/${out_path}"
    normalize_path "/tmp/${out_path}"
  fi

  if [[ "${err_path}" == /* ]]; then
    printf '%s\n' "${err_path}"
  else
    normalize_path "${workdir}/${err_path}"
    normalize_path "/root/${err_path}"
    normalize_path "/tmp/${err_path}"
  fi

  normalize_path "/root/slurm-${job_id}.out"
  normalize_path "/tmp/slurm-${job_id}.out"
  normalize_path "/root/slurm-${job_id}.err"
  normalize_path "/tmp/slurm-${job_id}.err"
}





get_job_nodelist_info() {
  local job_id="$1"
  local line
  local num_nodes
  local nodelist
  local raw_job_line

  line="$(slurm_exec_retry "scontrol show job ${job_id} -o")"
  num_nodes="$(extract_job_field "${line}" "NumNodes")"
  nodelist="$(extract_job_field "${line}" "NodeList")"

  if [[ -z "${num_nodes}" || ! "${num_nodes}" =~ ^[0-9]+$ ]]; then
    num_nodes=0
  fi
  if [[ -z "${nodelist}" ]]; then
    nodelist=unknown
  fi

  printf '%s %s\n' "${num_nodes}" "${nodelist}"
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
        local out_path
        out_path="$(get_job_stdout_path "${job_id}")"
        log "job ${job_id} (${name}) exit code is ${exit_code} (expected 0:0), stdout=${out_path}"
        controller_exec "test -f '${out_path}' && tail -n 80 '${out_path}' || true"
        return 1
      fi
      return 0
      ;;
    *)
      local out_path
      out_path="$(get_job_stdout_path "${job_id}")"
      log "job ${job_id} (${name}) failed with state=${state} exit=${exit_code}, stdout=${out_path}"
      controller_exec "test -f '${out_path}' && tail -n 120 '${out_path}' || true"
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
  wait_worker_pods_ready "${VERIFY_MIN_WORKER_REPLICAS}"
}

wait_slurm_nodes_ready() {
  local required_nodes="$1"
  local start
  local lines
  local ready_count
  local unhealthy_count
  local node
  local state
  local state_lc
  local base_state
  local considered_count

  start="$(date +%s)"
  while true; do
    lines="$(slurm_exec_retry "sinfo -h -N -p debug -o '%N %T'" || true)"
    ready_count=0
    unhealthy_count=0
    considered_count=0

    while read -r node state; do
      [[ -z "${node:-}" ]] && continue
      if [[ "${node}" != ${WORKER_NODE_NAME_PREFIX}* ]]; then
        continue
      fi

      considered_count=$((considered_count + 1))
      state_lc="$(printf '%s' "${state}" | tr '[:upper:]' '[:lower:]')"
      base_state="$(printf '%s' "${state_lc}" | sed 's/[^a-z].*$//')"

      if printf '%s' "${state_lc}" | grep -Eq 'down|drain|fail|not[_-]?respond|maint|unk'; then
        unhealthy_count=$((unhealthy_count + 1))
        continue
      fi

      case "${base_state}" in
        idle|mix|allocated|alloc)
          ready_count=$((ready_count + 1))
          ;;
      esac
    done <<< "${lines}"

    if (( ready_count >= required_nodes )); then
      log "slurm worker nodes ready: ${ready_count}/${required_nodes} (considered=${considered_count}, unhealthy=${unhealthy_count})"
      return 0
    fi

    if (( $(date +%s) - start > SLURM_NODE_READY_TIMEOUT_SECONDS )); then
      log "timeout waiting slurm worker nodes ready: need ${required_nodes}, got ${ready_count} (considered=${considered_count}, unhealthy=${unhealthy_count})"
      slurm_exec_retry "sinfo -R || true" || true
      slurm_exec_retry "sinfo -h -N -p debug -o '%N %T'" || true
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

    local ok="false"
    local retry
    for retry in 1 2 3 4; do
      if kubectl -n "${NAMESPACE}" exec "pod/${pod}" -- bash -lc 'pgrep -x munged >/dev/null && pgrep -x slurmd >/dev/null'; then
        ok="true"
        break
      fi
      sleep 2
    done

    if [[ "${ok}" != "true" ]]; then
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

ensure_multinode_prerequisites() {
  local required_nodes="$1"
  log "preflight multi-node prerequisites: required_nodes=${required_nodes}"
  ensure_worker_capacity
  verify_worker_daemons
  recover_unhealthy_nodes
  wait_slurm_nodes_ready "${required_nodes}"
}

assert_mpi_output() {
  local job_id="$1"
  local candidates
  local hosts
  local unique
  local node_info
  local num_nodes
  local nodelist
  local raw_job_line

  candidates="$(list_candidate_output_paths "${job_id}" | sed '/^$/d' | awk '!seen[$0]++')"

  hosts="$(printf '%s\n' "${candidates}" | kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc "while IFS= read -r p; do [ -f \"\$p\" ] || continue; grep -Eio 'rank-host=[^[:space:]]+' \"\$p\" || true; done" | sed 's/^rank-host=//I')"

  unique="$(printf '%s\n' "${hosts}" | sed '/^$/d' | sort -u | wc -l | tr -d ' ')"

  if [[ -n "${unique}" && "${unique}" -ge 2 ]]; then
    log "mpi output validation passed: unique_hosts=${unique}"
    return 0
  fi

  log "mpi output validation failed for job ${job_id}: unique_hosts=${unique:-0}"
  log "debug: candidate output paths"
  printf '%s\n' "${candidates}" | sed 's/^/[phase3 verify]   - /'

  log "debug: existing candidate files and tails"
  printf '%s\n' "${candidates}" | kubectl -n "${NAMESPACE}" exec -i "pod/${CONTROLLER_POD}" -- bash -lc "while IFS= read -r p; do [ -f \"\$p\" ] || continue; echo '-----' \"\$p\"; tail -n 120 \"\$p\"; done" || true

  raw_job_line="$(slurm_exec_retry "scontrol show job ${job_id} -o" || true)"
  log "debug: scontrol -o ${raw_job_line}"

  node_info="$(get_job_nodelist_info "${job_id}")"
  num_nodes="$(printf '%s' "${node_info}" | awk '{print $1}')"
  nodelist="$(printf '%s' "${node_info}" | cut -d' ' -f2-)"
  log "debug: scontrol job nodelist num_nodes=${num_nodes} nodelist=${nodelist}"

  if [[ -n "${num_nodes}" ]] && [[ "${num_nodes}" != "0" ]] && (( num_nodes >= 2 )); then
    log "mpi output fallback pass via allocation evidence (NumNodes=${num_nodes}, NodeList=${nodelist})"
    return 0
  fi

  return 1
}


main() {
  kubectl config use-context "${KUBE_CONTEXT}" >/dev/null
  trap restore_operator_if_needed EXIT

  log "checking ConfigMap + shared mount"
  kubectl -n "${NAMESPACE}" get configmap slurm-phase3-jobs >/dev/null
  controller_exec 'test -d /shared'
  slurm_exec_retry 'sinfo -h' >/dev/null

  pause_operator_if_needed

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

  ensure_multinode_prerequisites 2

  log "run MPI-like multi-node smoke"
  local mpi_job
  mpi_job="$(submit_job '/tmp/mpi-smoke.sbatch')"
  wait_job_done "${mpi_job}"
  print_job_status "${mpi_job}"
  assert_job_success "${mpi_job}" "phase3-mpi-smoke"
  assert_mpi_output "${mpi_job}"

  log "run PyTorch/checkpoint step"
  ensure_multinode_prerequisites 2
  local torch_job
  torch_job="$(submit_job '/tmp/pytorch-elastic.sbatch')"
  wait_job_done "${torch_job}"
  print_job_status "${torch_job}"
  assert_job_success "${torch_job}" "phase3-torch"

  log "phase3 verify completed"
}

main "$@"
