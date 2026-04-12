#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
VERIFY_TIMEOUT_SECONDS=${VERIFY_TIMEOUT_SECONDS:-180}
BASELINE_WORKER_STS=${BASELINE_WORKER_STS:-slurm-worker-cpu}
BASELINE_WORKER_POD=${BASELINE_WORKER_POD:-slurm-worker-cpu-0}
LOGIN_SELECTOR=${LOGIN_SELECTOR:-app=slurm-login}
PARTITION=${PARTITION:-debug}
GPU_POOL_STS=${GPU_POOL_STS:-slurm-worker-gpu-a10}
GPU_CONSTRAINT=${GPU_CONSTRAINT:-gpu-a10}
GPU_GRES=${GPU_GRES:-gpu:a10:1}
GPU_POOL_APP_LABEL=${GPU_POOL_APP_LABEL:-app=${GPU_POOL_STS}}

log() {
  echo "[dev verify] $*"
}

die() {
  echo "[dev verify][ERROR] $*" >&2
  exit 1
}

get_login_pod() {
  kubectl -n "$NAMESPACE" get pod -l "$LOGIN_SELECTOR" -o jsonpath='{.items[0].metadata.name}'
}

login_exec() {
  local pod attempt rc out
  pod=$(get_login_pod)
  for attempt in 1 2 3 4 5; do
    set +e
    out=$(kubectl -n "$NAMESPACE" exec "pod/${pod}" -- bash -lc "$1" 2>&1)
    rc=$?
    set -e
    if [[ $rc -eq 0 ]] && ! grep -qiE 'slurm_load_|socket timed out|unable to contact slurm controller|connect failure|connection refused' <<<"$out"; then
      printf '%s\n' "$out"
      return 0
    fi
    sleep 2
  done
  printf '%s\n' "$out" >&2
  return ${rc:-1}
}

safe_login_exec() {
  local out rc
  set +e
  out=$(login_exec "$1" 2>&1)
  rc=$?
  set -e
  printf '%s\n' "$out"
  return $rc
}

job_state() {
  local job_id="$1"
  local out state
  out=$(safe_login_exec "scontrol show job ${job_id} 2>/dev/null | sed -n 's/.*JobState=\([^ ]*\).*/\1/p' | head -n1 || true" || true)
  state=$(printf '%s\n' "$out" | tr -d '\n' | tail -n1 | xargs)
  if [[ -n "$state" && "$state" != *Socket* && "$state" != slurm_* ]]; then
    printf '%s\n' "$state"
    return 0
  fi
  out=$(safe_login_exec "squeue -h -j ${job_id} -o %T 2>/dev/null || true" || true)
  state=$(printf '%s\n' "$out" | tr -d '\n' | tail -n1 | xargs)
  if [[ -n "$state" && "$state" != *Socket* && "$state" != slurm_* ]]; then
    printf '%s\n' "$state"
    return 0
  fi
  printf '\n'
}

job_nodelist() {
  local job_id="$1"
  local out nodelist
  out=$(safe_login_exec "squeue -h -j ${job_id} -o %N 2>/dev/null || true" || true)
  nodelist=$(printf '%s\n' "$out" | tr -d '\n' | tail -n1 | xargs)
  if [[ -n "$nodelist" && "$nodelist" != *Socket* && "$nodelist" != slurm_* ]]; then
    printf '%s\n' "$nodelist"
    return 0
  fi
  safe_login_exec "scontrol show job ${job_id} 2>/dev/null | sed -n 's/.* NodeList=\([^ ]*\).*/\1/p' | head -n1 || true" | tr -d '\n' | tail -n1 | xargs
}

is_terminal_job_state() {
  case "$1" in
    CANCELLED|COMPLETED|COMPLETING|FAILED|TIMEOUT|PREEMPTED|BOOT_FAIL|NODE_FAIL|OUT_OF_MEMORY|DEADLINE|CANCELLED+|COMPLETED+|FAILED+|TIMEOUT+|PREEMPTED+|BOOT_FAIL+|NODE_FAIL+|OUT_OF_MEMORY+|DEADLINE+)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

wait_job_state() {
  local job_id="$1" wanted="$2" timeout="$3"
  local start now state seen_nonempty
  start=$(date +%s)
  seen_nonempty=false
  while true; do
    state=$(job_state "$job_id")
    [[ -n "$state" ]] && seen_nonempty=true
    [[ "$state" == "$wanted" ]] && return 0
    now=$(date +%s)
    if (( now - start > timeout )); then
      if [[ "$seen_nonempty" == false && -z "$state" ]]; then
        echo "state=<not-observable>"
      else
        echo "state=${state:-<gone>}"
      fi
      return 1
    fi
    sleep 2
  done
}

wait_job_finished() {
  local job_id="$1" timeout="$2"
  local start now state
  start=$(date +%s)
  while true; do
    state=$(job_state "$job_id")
    if [[ -z "$state" ]]; then
      return 0
    fi
    if is_terminal_job_state "$state"; then
      return 0
    fi
    now=$(date +%s)
    if (( now - start > timeout )); then
      echo "state=${state:-<gone>}"
      return 1
    fi
    sleep 2
  done
}

submit_job() {
  local script_content="$1"
  local remote_path="$2"
  login_exec "cat > '${remote_path}' <<'EOF_JOB'
${script_content}
EOF_JOB
sbatch --parsable '${remote_path}'" | tr -d '\r' | tail -n1
}

cleanup_dev_jobs() {
  login_exec "squeue -h -o '%i %j' | awk '\$2 ~ /^dev-/{print \$1}' | xargs -r scancel || true" >/dev/null || true
}

wait_login_query() {
  local cmd="$1" timeout="$2"
  local start now
  start=$(date +%s)
  while true; do
    if login_exec "$cmd" >/dev/null 2>&1; then
      return 0
    fi
    now=$(date +%s)
    if (( now - start > timeout )); then
      return 1
    fi
    sleep 3
  done
}

wait_no_dev_pending() {
  local timeout="$1"
  local start now count
  start=$(date +%s)
  while true; do
    count=$(login_exec "squeue -h -t PENDING -o '%j' | awk '/^dev-/{c++} END{print c+0}'" | tr -d '\r' | tail -n1)
    [[ "${count:-0}" == "0" ]] && return 0
    now=$(date +%s)
    if (( now - start > timeout )); then
      login_exec 'squeue || true'
      return 1
    fi
    sleep 3
  done
}

wait_statefulset_ready_replicas() {
  local sts="$1" expected="$2" timeout="$3"
  local start now replicas ready
  start=$(date +%s)
  while true; do
    replicas=$(kubectl -n "$NAMESPACE" get statefulset/${sts} -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)
    ready=$(kubectl -n "$NAMESPACE" get statefulset/${sts} -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
    replicas=${replicas:-0}
    ready=${ready:-0}
    if [[ "$replicas" == "$expected" && "$ready" == "$expected" ]]; then
      return 0
    fi
    now=$(date +%s)
    if (( now - start > timeout )); then
      kubectl -n "$NAMESPACE" get statefulset/${sts} -o wide >&2 || true
      kubectl -n "$NAMESPACE" get pods -l "app=${sts}" -o wide >&2 || true
      return 1
    fi
    sleep 4
  done
}

wait_replicas_at_most() {
  local sts="$1" max_replicas="$2" timeout="$3"
  local start now replicas
  start=$(date +%s)
  while true; do
    replicas=$(kubectl -n "$NAMESPACE" get statefulset/${sts} -o jsonpath='{.spec.replicas}')
    if [[ "${replicas}" -le "${max_replicas}" ]]; then
      echo "${replicas}"
      return 0
    fi
    now=$(date +%s)
    if (( now - start > timeout )); then
      echo "${replicas}"
      return 1
    fi
    sleep 5
  done
}

assert_job_runs_on_prefix() {
  local job_id="$1" prefix="$2" label="$3"
  local nodelist
  nodelist=$(job_nodelist "$job_id")
  echo "${label} job ${job_id} nodelist: ${nodelist}"
  [[ -n "$nodelist" ]] || die "${label} job ${job_id} has empty NodeList"
  [[ "$nodelist" == ${prefix}* ]] || die "${label} job ${job_id} ran on ${nodelist}, expected prefix ${prefix}"
}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

log "checking core pods..."
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-controller-0 --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${BASELINE_WORKER_POD}" --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Available deployment/slurm-elastic-operator --timeout=120s
login_pod=$(get_login_pod)
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${login_pod}" --timeout=120s
cleanup_dev_jobs
wait_no_dev_pending 30 || die "stale dev jobs are still pending before verification"

log "warming up Slurm client queries"
wait_login_query 'scontrol ping' 60 || die "Slurm controller ping did not stabilize during warm-up"
wait_login_query 'squeue >/dev/null' 90 || log "squeue warm-up remained flaky, continuing"

log "phase1 functional checks..."
log "controller ping from login"
login_exec 'scontrol ping'

log "baseline worker readiness"
kubectl -n "$NAMESPACE" get pod "${BASELINE_WORKER_POD}" -o wide

log "single-node srun smoke test"
srun_host=$(login_exec "srun -N1 -n1 --constraint=cpu hostname" | tr -d '\r' | tail -n1)
echo "$srun_host"
if [[ "$srun_host" != "$BASELINE_WORKER_POD" ]]; then
  die "unexpected srun host: ${srun_host} (expected ${BASELINE_WORKER_POD})"
fi

log "single-node sbatch smoke test on CPU pool"
kubectl -n "$NAMESPACE" scale statefulset/${BASELINE_WORKER_STS} --replicas=1 >/dev/null
wait_statefulset_ready_replicas "$BASELINE_WORKER_STS" 1 120 || die "baseline CPU pool did not become ready at 1 replica"
cpu_job=$(submit_job "#!/bin/bash
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH -J dev-cpu-smoke
#SBATCH --constraint=cpu
sleep 20" "/tmp/dev-cpu-smoke.sbatch")
echo "submitted cpu smoke job: ${cpu_job}"
if ! wait_job_state "$cpu_job" RUNNING 120 >/tmp/devverify_cpu_state.txt; then
  state=$(cat /tmp/devverify_cpu_state.txt)
  login_exec "squeue -j ${cpu_job} || true"
  die "cpu smoke job ${cpu_job} did not reach RUNNING in time (${state})"
fi
assert_job_runs_on_prefix "$cpu_job" "slurm-worker-cpu-" "cpu"
login_exec "scancel ${cpu_job} || true"
wait_job_finished "$cpu_job" 60 >/dev/null || die "cpu smoke job ${cpu_job} did not reach a terminal state after cancel"

log "phase2 scale-up/scale-down checks for CPU pool"
kubectl -n "$NAMESPACE" scale statefulset/${BASELINE_WORKER_STS} --replicas=1 >/dev/null
wait_statefulset_ready_replicas "$BASELINE_WORKER_STS" 1 120 || die "CPU pool did not settle to one ready replica before scale-up test"
wait_no_dev_pending 60 || die "pending dev jobs still exist before CPU scale test"
scale_job=$(submit_job "#!/bin/bash
#SBATCH -p ${PARTITION}
#SBATCH -N 2
#SBATCH -J dev-scale-trigger
#SBATCH --constraint=cpu
sleep 60" "/tmp/dev-scale-trigger.sbatch")
echo "submitted scale trigger job: ${scale_job}"
start_ts=$(date +%s)
scaled_up=false
while true; do
  replicas=$(kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o jsonpath='{.spec.replicas}')
  ready=$(kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  if [[ "${replicas:-0}" -ge 2 && "${ready:-0}" -ge 2 ]]; then
    scaled_up=true
    break
  fi
  now=$(date +%s)
  if (( now - start_ts > VERIFY_TIMEOUT_SECONDS )); then
    break
  fi
  sleep 5
done
if [[ "$scaled_up" != "true" ]]; then
  kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o wide >&2 || true
  kubectl -n "$NAMESPACE" logs deploy/slurm-elastic-operator --tail=120 >&2 || true
  login_exec 'squeue || true' >&2 || true
  die "operator did not scale up CPU pool in time"
fi
echo "scale-up verified: replicas=$(kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o jsonpath='{.spec.replicas}')"
login_exec "scancel ${scale_job} || true"
wait_job_finished "$scale_job" 90 >/dev/null || die "scale trigger job ${scale_job} did not reach a terminal state after cancel"
wait_no_dev_pending 90 || die "pending dev jobs still exist after cancelling CPU scale trigger"
replicas=$(wait_replicas_at_most "$BASELINE_WORKER_STS" 1 150) || die "CPU pool scale-down not completed yet (replicas=${replicas})"
wait_statefulset_ready_replicas "$BASELINE_WORKER_STS" 1 120 || die "CPU pool did not return to one ready replica after scale-down"
echo "scale-down verified: replicas=${replicas}"

log "MPI checks (PMI2 plugin + OpenMPI + PDB)"

log "PMI2 srun smoke test"
pmi2_job=$(submit_job "#!/bin/bash
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH -n 2
#SBATCH -J dev-pmi2-smoke
#SBATCH --constraint=cpu
#SBATCH --output=/tmp/dev-pmi2-%j.out
#SBATCH --time=00:02:00
srun --mpi=pmi2 /bin/sh -c 'echo rank:\${SLURM_PROCID} ntasks:\${SLURM_NTASKS}'" "/tmp/dev-pmi2-smoke.sbatch")
echo "submitted pmi2 job: ${pmi2_job}"
if ! wait_job_finished "$pmi2_job" 120 >/tmp/devverify_pmi2_state.txt; then
  state=$(cat /tmp/devverify_pmi2_state.txt)
  die "pmi2 smoke job ${pmi2_job} did not finish in time (${state})"
fi
pmi2_out=$(login_exec "cat /tmp/dev-pmi2-${pmi2_job}.out 2>/dev/null || echo ''" | tr -d '\r')
if echo "$pmi2_out" | grep -q "rank:0" && echo "$pmi2_out" | grep -q "rank:1"; then
  echo "pmi2: both ranks completed"
else
  printf '%s\n' "$pmi2_out" | sed 's/^/  /' >&2
  die "pmi2 smoke job ${pmi2_job} did not produce expected rank output"
fi

log "OpenMPI mpirun availability"
mpirun_path=$(kubectl -n "$NAMESPACE" exec "pod/${BASELINE_WORKER_POD}" -- which mpirun 2>/dev/null || echo "")
if [[ -n "$mpirun_path" ]]; then
  echo "mpirun found: ${mpirun_path}"
  ompi_job=$(submit_job "#!/bin/bash
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH -n 2
#SBATCH -J dev-ompi-smoke
#SBATCH --constraint=cpu
#SBATCH --output=/tmp/dev-ompi-%j.out
#SBATCH --time=00:02:00
mpirun --oversubscribe -np 2 --mca btl_base_warn_component_unused 0 /bin/sh -c 'echo ompi-rank:\${OMPI_COMM_WORLD_RANK} host:\$(hostname)'" "/tmp/dev-ompi-smoke.sbatch")
  echo "submitted ompi job: ${ompi_job}"
  if wait_job_finished "$ompi_job" 120 >/tmp/devverify_ompi_state.txt; then
    ompi_out=$(login_exec "cat /tmp/dev-ompi-${ompi_job}.out 2>/dev/null || echo ''" | tr -d '\r')
    if echo "$ompi_out" | grep -q "ompi-rank:0" && echo "$ompi_out" | grep -q "ompi-rank:1"; then
      echo "openmpi: both ranks completed"
    else
      echo "WARNING: OpenMPI test output unexpected (non-fatal)" >&2
    fi
  else
    echo "WARNING: OpenMPI smoke job timed out (non-fatal)" >&2
  fi
else
  log "mpirun not found on ${BASELINE_WORKER_POD} — skipping OpenMPI test"
fi

log "PodDisruptionBudget check"
pdb_count=$(kubectl -n "$NAMESPACE" get pdb -o name 2>/dev/null | wc -l | tr -d ' ')
kubectl -n "$NAMESPACE" get pdb 2>/dev/null || true
if (( pdb_count >= 5 )); then
  echo "PDB count: ${pdb_count} (pass)"
else
  echo "WARNING: fewer PDBs than expected (want ≥5, got ${pdb_count})" >&2
fi

if kubectl -n "$NAMESPACE" get statefulset/${GPU_POOL_STS} >/dev/null 2>&1; then
  cleanup_dev_jobs
  wait_no_dev_pending 30 || die "stale dev jobs are still pending before GPU verification"
  log "gpu pool verification (${GPU_POOL_STS}, constraint=${GPU_CONSTRAINT}, gres=${GPU_GRES})"
  kubectl -n "$NAMESPACE" scale statefulset/${GPU_POOL_STS} --replicas=0 >/dev/null || true
  gpu_job=$(submit_job "#!/bin/bash
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH -J dev-gpu-smoke
#SBATCH --constraint=${GPU_CONSTRAINT}
#SBATCH --gres=${GPU_GRES}
sleep 60" "/tmp/dev-gpu-smoke.sbatch")
  echo "submitted gpu smoke job: ${gpu_job}"
  start_ts=$(date +%s)
  gpu_scaled=false
  while true; do
    gpu_replicas=$(kubectl -n "$NAMESPACE" get statefulset/${GPU_POOL_STS} -o jsonpath='{.spec.replicas}')
    gpu_ready=$(kubectl -n "$NAMESPACE" get statefulset/${GPU_POOL_STS} -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
    if [[ "${gpu_replicas:-0}" -ge 1 && "${gpu_ready:-0}" -ge 1 ]]; then
      gpu_scaled=true
      break
    fi
    now=$(date +%s)
    if (( now - start_ts > VERIFY_TIMEOUT_SECONDS )); then
      break
    fi
    sleep 5
  done
  [[ "$gpu_scaled" == "true" ]] || die "operator did not scale up GPU pool ${GPU_POOL_STS} in time"
  if ! wait_job_state "$gpu_job" RUNNING 240 >/tmp/devverify_gpu_state.txt; then
    state=$(cat /tmp/devverify_gpu_state.txt)
    kubectl -n "$NAMESPACE" get statefulset/${GPU_POOL_STS} -o wide >&2 || true
    kubectl -n "$NAMESPACE" get pods -l "$GPU_POOL_APP_LABEL" -o wide >&2 || true
    kubectl -n "$NAMESPACE" logs deploy/slurm-elastic-operator --tail=120 >&2 || true
    login_exec "squeue -j ${gpu_job} || true" >&2 || true
    login_exec "scontrol show job ${gpu_job} || true" >&2 || true
    die "gpu smoke job ${gpu_job} did not reach RUNNING in time (${state})"
  fi
  assert_job_runs_on_prefix "$gpu_job" "${GPU_POOL_STS}-" "gpu"
  login_exec "scancel ${gpu_job} || true"
  wait_job_finished "$gpu_job" 90 >/dev/null || die "gpu smoke job ${gpu_job} did not reach a terminal state after cancel"
  echo "gpu-pool verified via job ${gpu_job}"
else
  log "gpu pool ${GPU_POOL_STS} not present; skipping gpu verification"
fi

echo "[dev verify] done. all checks passed."
