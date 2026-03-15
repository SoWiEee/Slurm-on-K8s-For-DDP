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
  local pod
  pod=$(get_login_pod)
  kubectl -n "$NAMESPACE" exec "pod/${pod}" -- bash -lc "$1"
}

job_state() {
  local job_id="$1"
  login_exec "squeue -h -j ${job_id} -o %T 2>/dev/null || true" | tr -d '\r' | tail -n1
}

job_nodelist() {
  local job_id="$1"
  login_exec "scontrol show job ${job_id} 2>/dev/null | sed -n 's/.* NodeList=\([^ ]*\).*/\1/p' | head -n1 || true" | tr -d '\r' | xargs
}

wait_job_state() {
  local job_id="$1" wanted="$2" timeout="$3"
  local start now state
  start=$(date +%s)
  while true; do
    state=$(job_state "$job_id")
    [[ "$state" == "$wanted" ]] && return 0
    now=$(date +%s)
    if (( now - start > timeout )); then
      echo "state=${state:-<gone>}"
      return 1
    fi
    sleep 2
  done
}

wait_job_gone() {
  local job_id="$1" timeout="$2"
  local start now state
  start=$(date +%s)
  while true; do
    state=$(job_state "$job_id")
    [[ -z "$state" ]] && return 0
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

log "phase1 functional checks..."
log "controller ping from login"
login_exec 'scontrol ping'

log "partition and node inventory"
login_exec 'sinfo'
login_exec "scontrol show node ${BASELINE_WORKER_POD}"

log "single-node srun smoke test"
srun_host=$(login_exec "srun -N1 -n1 --constraint=cpu hostname" | tr -d '\r' | tail -n1)
echo "$srun_host"
if [[ "$srun_host" != "$BASELINE_WORKER_POD" ]]; then
  die "unexpected srun host: ${srun_host} (expected ${BASELINE_WORKER_POD})"
fi

log "single-node sbatch smoke test on CPU pool"
kubectl -n "$NAMESPACE" scale statefulset/${BASELINE_WORKER_STS} --replicas=1 >/dev/null
kubectl -n "$NAMESPACE" rollout status statefulset/${BASELINE_WORKER_STS} --timeout=120s >/dev/null
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
wait_job_gone "$cpu_job" 60 >/dev/null || die "cpu smoke job ${cpu_job} did not leave queue after cancel"

log "phase2 scale-up/scale-down checks for CPU pool"
kubectl -n "$NAMESPACE" scale statefulset/${BASELINE_WORKER_STS} --replicas=1 >/dev/null
kubectl -n "$NAMESPACE" rollout status statefulset/${BASELINE_WORKER_STS} --timeout=120s >/dev/null
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
  if [[ "${replicas}" -ge 2 ]]; then
    scaled_up=true
    break
  fi
  now=$(date +%s)
  if (( now - start_ts > VERIFY_TIMEOUT_SECONDS )); then
    break
  fi
  sleep 5
done
[[ "$scaled_up" == "true" ]] || die "operator did not scale up CPU pool in time"
echo "scale-up verified: replicas=$(kubectl -n "$NAMESPACE" get statefulset/${BASELINE_WORKER_STS} -o jsonpath='{.spec.replicas}')"
login_exec "scancel ${scale_job} || true"
wait_job_gone "$scale_job" 90 >/dev/null || die "scale trigger job ${scale_job} did not leave queue after cancel"
wait_no_dev_pending 90 || die "pending dev jobs still exist after cancelling CPU scale trigger"
replicas=$(wait_replicas_at_most "$BASELINE_WORKER_STS" 1 150) || die "CPU pool scale-down not completed yet (replicas=${replicas})"
echo "scale-down verified: replicas=${replicas}"

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
    if [[ "${gpu_replicas}" -ge 1 ]]; then
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
  kubectl -n "$NAMESPACE" rollout status statefulset/${GPU_POOL_STS} --timeout=180s >/dev/null || true
  login_exec 'scontrol reconfigure || true'
  gpu_node="${GPU_POOL_STS}-0"
  start_ts=$(date +%s)
  while true; do
    if login_exec "scontrol show node ${gpu_node} >/dev/null 2>&1 && echo ok || true" | grep -q ok; then
      break
    fi
    now=$(date +%s)
    if (( now - start_ts > 120 )); then
      login_exec "sinfo -N -l || true"
      die "gpu node ${gpu_node} did not appear in Slurm inventory after scale-up"
    fi
    sleep 3
  done
  if ! wait_job_state "$gpu_job" RUNNING 180 >/tmp/devverify_gpu_state.txt; then
    state=$(cat /tmp/devverify_gpu_state.txt)
    login_exec "squeue -j ${gpu_job} || true"
    die "gpu smoke job ${gpu_job} did not reach RUNNING in time (${state})"
  fi
  assert_job_runs_on_prefix "$gpu_job" "${GPU_POOL_STS}-" "gpu"
  login_exec "scancel ${gpu_job} || true"
  wait_job_gone "$gpu_job" 90 >/dev/null || die "gpu smoke job ${gpu_job} did not leave queue after cancel"
  echo "gpu-pool verified via job ${gpu_job}"
else
  log "gpu pool ${GPU_POOL_STS} not present; skipping gpu verification"
fi

echo "[dev verify] done. phase1 + phase2 checks passed."
