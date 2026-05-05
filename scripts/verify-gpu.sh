#!/usr/bin/env bash
# verify-gpu.sh — Verify real GPU access via Slurm
#
# Prerequisites:
#   - scripts/install-gpu-operator.sh has installed NVIDIA GPU Operator
#     into the gpu-operator namespace
#   - helm install slurm-platform -f chart/values-k3s.yaml has deployed the
#     core Slurm cluster (which contributes the device-plugin-config ConfigMap
#     and post-install node-labeler Job).
#
# Tests:
#   1. NVIDIA device plugin DaemonSet is running
#   2. At least one node advertises nvidia.com/gpu
#   3. GPU worker pod can run nvidia-smi
#   4. Slurm GPU GRES is visible (sinfo --Node --Format)
#   5. sbatch GPU job runs nvidia-smi inside the job

set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
K8S_RUNTIME=${K8S_RUNTIME:-kind}
KUBE_CONTEXT=${KUBE_CONTEXT:-$([[ "$K8S_RUNTIME" == "k3s" ]] && echo "default" || echo "kind-${CLUSTER_NAME}")}
GPU_POOL_STS=${GPU_POOL_STS:-slurm-worker-gpu-rtx4070}
GPU_CONSTRAINT=${GPU_CONSTRAINT:-gpu-rtx4070}
GPU_GRES=${GPU_GRES:-gpu:rtx4070:1}
JOB_TIMEOUT=${JOB_TIMEOUT:-120}
PARTITION=${PARTITION:-gpu-rtx4070}
CLEANUP_STALE_VERIFY_JOBS=${CLEANUP_STALE_VERIFY_JOBS:-true}
# MPS test (only meaningful on the rtx4070 pool, which has sharing.mps enabled).
MPS_PARTITION=${MPS_PARTITION:-gpu-rtx4070}
MPS_GRES=${MPS_GRES:-mps:25}
SKIP_MPS=${SKIP_MPS:-false}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

pass() { echo "  PASS: $*"; }
fail() { echo "  FAIL: $*" >&2; exit 1; }
warn() { echo "  WARN: $*" >&2; }

login_exec() {
  local pod
  pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "slurm-controller-0")
  kubectl -n "$NAMESPACE" exec "pod/${pod}" -- bash -lc "$1"
}

fix_node_addr() {
  local nodename="$1"
  local pod_ip
  pod_ip=$(kubectl -n "$NAMESPACE" get pod "$nodename" \
    -o jsonpath='{.status.podIP}' 2>/dev/null || true)
  if [[ -n "$pod_ip" ]]; then
    login_exec "scontrol update NodeName=${nodename} NodeAddr=${pod_ip} 2>/dev/null || true" >/dev/null
  fi
}

wait_slurm_node_responding() {
  local nodename="$1" timeout_s="${2:-90}"
  local deadline state elapsed sighup_sent
  deadline=$(( $(date +%s) + timeout_s ))
  elapsed=0
  sighup_sent=0

  while true; do
    state=$(login_exec "sinfo -N -h -n '${nodename}' -o '%T' 2>/dev/null" \
      2>/dev/null | tr -d '\r\n' || true)
    if [[ -n "$state" && "$state" != *"*" ]]; then
      echo "  ${nodename}: ${state}"
      return 0
    fi
    fix_node_addr "$nodename"
    if echo "$state" | grep -qiE "down|drain|fail|unknown"; then
      login_exec "scontrol update NodeName=${nodename} State=RESUME 2>/dev/null || true" >/dev/null
    fi
    echo "  ${nodename}: ${state:-unknown} (waiting for slurmd heartbeat)"
    if (( $(date +%s) >= deadline )); then
      return 1
    fi
    elapsed=$(( elapsed + 3 ))
    if [[ "$elapsed" -ge 30 && "$sighup_sent" -eq 0 ]]; then
      echo "  ${nodename}: refreshing NodeAddr and SIGHUP slurmd"
      fix_node_addr "$nodename"
      kubectl -n "$NAMESPACE" exec "pod/${nodename}" -- \
        bash -lc 'kill -HUP $(pgrep slurmd) 2>/dev/null || true' >/dev/null 2>&1 || true
      sighup_sent=1
    fi
    sleep 3
  done
}

cancel_job_quietly() {
  local jobid="$1"
  [[ -n "$jobid" ]] || return 0
  login_exec "scancel ${jobid} 2>/dev/null || true" >/dev/null 2>&1 || true
}

cleanup_verify_jobs() {
  [[ "$CLEANUP_STALE_VERIFY_JOBS" == "true" ]] || return 0
  login_exec "scancel --name=gpu-verify --partition=${PARTITION} 2>/dev/null || true; scancel --name=mps-verify --partition=${MPS_PARTITION} 2>/dev/null || true" \
    >/dev/null 2>&1 || true

  local deadline remaining
  deadline=$(( $(date +%s) + 30 ))
  while true; do
    remaining=$(login_exec \
      "squeue -h -p ${PARTITION},${MPS_PARTITION} -o '%j' 2>/dev/null | grep -Ec '^(gpu-verify|mps-verify)$' || true" \
      2>/dev/null | tr -d '\r' | tail -n1 || echo "0")
    [[ "${remaining:-0}" == "0" ]] && return 0
    (( $(date +%s) >= deadline )) && return 0
    sleep 2
  done
}

wait_gpu_pool_responding() {
  local timeout_s="${1:-120}"
  local deadline gpu_nodes nodename ok
  deadline=$(( $(date +%s) + timeout_s ))

  while true; do
    gpu_nodes=$(kubectl -n "$NAMESPACE" get pod -l "app=${GPU_POOL_STS}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null || true)
    if [[ -n "$gpu_nodes" ]]; then
      ok=true
      while IFS= read -r nodename; do
        [[ -n "$nodename" ]] || continue
        wait_slurm_node_responding "$nodename" 45 || ok=false
      done <<< "$gpu_nodes"
      [[ "$ok" == "true" ]] && return 0
    else
      echo "  waiting for ${GPU_POOL_STS} pod(s) to exist"
    fi

    (( $(date +%s) >= deadline )) && return 1
    sleep 3
  done
}

# ---------------------------------------------------------------------------
# [1] NVIDIA device plugin
# ---------------------------------------------------------------------------
echo "=== [1] NVIDIA device plugin DaemonSet ==="
ds_ready=$(kubectl -n kube-system get daemonset/nvidia-device-plugin-daemonset \
  -o jsonpath='{.status.numberReady}' 2>/dev/null || echo "0")
ds_desired=$(kubectl -n kube-system get daemonset/nvidia-device-plugin-daemonset \
  -o jsonpath='{.status.desiredNumberScheduled}' 2>/dev/null || echo "0")
echo "  Ready: ${ds_ready}/${ds_desired}"
if [[ "${ds_ready:-0}" -gt 0 ]]; then
  pass "device plugin DaemonSet has ready pods"
else
  fail "no ready device plugin pods (run scripts/install-gpu-operator.sh first)"
fi

# ---------------------------------------------------------------------------
# [2] Node GPU capacity
# ---------------------------------------------------------------------------
echo ""
echo "=== [2] GPU node capacity ==="
kubectl get nodes \
  -o custom-columns='NODE:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu' \
  2>/dev/null
gpu_total=$(kubectl get nodes \
  -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' \
  2>/dev/null | grep -v '^$' | awk '{s+=$1} END{print s+0}')
if [[ "${gpu_total:-0}" -gt 0 ]]; then
  pass "cluster has ${gpu_total} allocatable GPU(s)"
else
  warn "no GPUs advertised (check device plugin + driver)"
fi

# ---------------------------------------------------------------------------
# [3] nvidia-smi inside GPU worker pod
# ---------------------------------------------------------------------------
echo ""
echo "=== [3] nvidia-smi in GPU worker pod ==="
cleanup_verify_jobs
gpu_pod=$(kubectl -n "$NAMESPACE" get pod -l "app=${GPU_POOL_STS}" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
if [[ -z "$gpu_pod" ]]; then
  warn "no ${GPU_POOL_STS} pod running (operator may need to scale up); skipping"
else
  if kubectl -n "$NAMESPACE" exec "pod/${gpu_pod}" -- nvidia-smi \
      --query-gpu=name,driver_version,utilization.gpu \
      --format=csv,noheader 2>/dev/null | sed 's/^/  /'; then
    pass "nvidia-smi succeeded in ${gpu_pod}"
  else
    warn "nvidia-smi failed inside pod (check resources.limits nvidia.com/gpu)"
  fi
fi

# ---------------------------------------------------------------------------
# [4] Slurm GPU GRES visible
# ---------------------------------------------------------------------------
echo ""
echo "=== [4] Slurm GPU GRES (sinfo) ==="
login_exec "sinfo --Node --Format=NodeList,Gres,GresUsed,StateLong 2>/dev/null | head -20" || true

gpu_gres_count=$(login_exec \
  "sinfo --Node --Format=Gres --noheader 2>/dev/null | grep -c 'gpu' || echo 0" \
  2>/dev/null | tr -d '\r' | tail -n1 || echo "0")
if [[ "${gpu_gres_count:-0}" -gt 0 ]]; then
  pass "Slurm nodes have GPU GRES configured"
else
  warn "no GPU GRES in sinfo (check gres.conf / slurm.conf)"
fi

echo ""
echo "=== [4b] Slurm GPU worker heartbeat ==="
if ! kubectl -n "$NAMESPACE" get pod -l "app=${GPU_POOL_STS}" \
  -o jsonpath='{.items[0].metadata.name}' >/dev/null 2>&1; then
  warn "no ${GPU_POOL_STS} pods to check"
else
  wait_gpu_pool_responding 90 \
    || fail "${GPU_POOL_STS} stayed NOT_RESPONDING; check slurmd logs and NetworkPolicy"
  pass "GPU Slurm worker nodes are responding"
fi

# ---------------------------------------------------------------------------
# [5] sbatch GPU job — nvidia-smi inside Slurm job
# ---------------------------------------------------------------------------
echo ""
echo "=== [5] sbatch GPU job (nvidia-smi) ==="

cleanup_verify_jobs

submit_pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "slurm-controller-0")

SHARED_DIR=${SHARED_DIR:-/shared}
JOB_OUT_DIR="${SHARED_DIR}/gpu-verify-$$"

JOB_SCRIPT="#!/bin/bash
#SBATCH -J gpu-verify
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH --constraint=${GPU_CONSTRAINT}
#SBATCH --gres=${GPU_GRES}
#SBATCH --output=${JOB_OUT_DIR}/%j.out
#SBATCH --error=${JOB_OUT_DIR}/%j.err
#SBATCH --time=00:02:00
#SBATCH --no-requeue
nvidia-smi --query-gpu=name,driver_version,utilization.gpu --format=csv,noheader
echo \"CUDA_VISIBLE_DEVICES=\${CUDA_VISIBLE_DEVICES}\"
echo \"SLURM_JOB_GPUS=\${SLURM_JOB_GPUS}\""

jid=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
  bash -c "mkdir -p '${JOB_OUT_DIR}'; cat > /tmp/gpu-verify.sbatch <<'EOF_JOB'
${JOB_SCRIPT}
EOF_JOB
sbatch --parsable /tmp/gpu-verify.sbatch" 2>/dev/null | tr -d '\r' | tail -n1)

echo "  Submitted job: ${jid}"
wait_gpu_pool_responding 150 \
  || warn "${GPU_POOL_STS} did not become Slurm-ready before job polling"

deadline=$(( $(date +%s) + JOB_TIMEOUT ))
timed_out=false
while true; do
  state=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
    bash -lc "scontrol show job ${jid} 2>/dev/null | grep -oP 'JobState=\K\w+'" \
    2>/dev/null | tr -d '\r' | tail -n1 || echo "")
  printf "  [%s] job %s state=%s\n" "$(date +%H:%M:%S)" "$jid" "${state:-?}"
  case "${state:-}" in
    COMPLETED|FAILED|CANCELLED|TIMEOUT|NODE_FAIL) break ;;
  esac
  if (( $(date +%s) >= deadline )); then
    warn "timed out waiting for GPU job ${jid}"
    timed_out=true
    cancel_job_quietly "$jid"
    break
  fi
  sleep 4
done

out=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
  bash -c "cat '${JOB_OUT_DIR}/${jid}.out' 2>/dev/null || echo ''" \
  2>/dev/null | tr -d '\r')
err=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
  bash -c "cat '${JOB_OUT_DIR}/${jid}.err' 2>/dev/null || echo ''" \
  2>/dev/null | tr -d '\r')

echo "  Job stdout:"
echo "$out" | sed 's/^/    /'
if [[ -n "$err" ]]; then
  echo "  Job stderr:"
  echo "$err" | sed 's/^/    /' >&2
fi

if echo "$out" | grep -qi "nvidia\|tesla\|rtx\|a10\|h100\|v100"; then
  pass "GPU name visible in Slurm job output"
elif [[ "$state" == "COMPLETED" ]]; then
  # Without shared storage (NFS), output files land on the worker pod which may
  # have scaled down by the time we read. Fall back to scontrol to verify the
  # job actually consumed a GPU GRES and exited cleanly.
  job_info=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
    bash -lc "scontrol show job ${jid} 2>/dev/null" 2>/dev/null | tr -d '\r' || true)
  exit_code=$(echo "$job_info" | grep -oP 'ExitCode=\K[^ ]+' || true)
  gpu_tres=$(echo "$job_info"  | grep -oP 'TresPerNode=\Kgres:gpu[^ ]+' || true)
  echo "  (output file not readable from login — check /shared mount)"
  echo "  ExitCode=${exit_code}  TresPerNode=${gpu_tres}"
  if [[ "$exit_code" == "0:0" && -n "$gpu_tres" ]]; then
    pass "GPU job ExitCode=0:0 and GPU GRES allocated (${gpu_tres})"
  else
    warn "job completed but ExitCode='${exit_code}' or no GPU GRES in scontrol (${gpu_tres})"
  fi
else
  if [[ "$timed_out" == "true" ]]; then
    job_reason=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
      bash -lc "scontrol show job ${jid} 2>/dev/null | grep -oP 'Reason=\K[^ ]+' || true" \
      2>/dev/null | tr -d '\r' | tail -n1 || true)
    [[ -n "$job_reason" ]] && echo "  Last pending reason: ${job_reason}"
  fi
  fail "GPU job did not complete (state=${state})"
fi

# ---------------------------------------------------------------------------
# [6] sbatch MPS job — verify --gres=mps:N path (CUDA_MPS_ACTIVE_THREAD_PERCENTAGE)
# ---------------------------------------------------------------------------
echo ""
echo "=== [6] sbatch MPS job (--gres=${MPS_GRES}) ==="
if [[ "$SKIP_MPS" == "true" ]]; then
  warn "SKIP_MPS=true — skipping MPS verification"
else
  MPS_OUT_DIR="${SHARED_DIR}/mps-verify-$$"

  MPS_JOB="#!/bin/bash
#SBATCH -J mps-verify
#SBATCH -p ${MPS_PARTITION}
#SBATCH -N 1
#SBATCH --gres=${MPS_GRES}
#SBATCH --output=${MPS_OUT_DIR}/%j.out
#SBATCH --error=${MPS_OUT_DIR}/%j.err
#SBATCH --time=00:02:00
#SBATCH --no-requeue
echo \"CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=\${CUDA_MPS_ACTIVE_THREAD_PERCENTAGE:-unset}\"
echo \"CUDA_MPS_PIPE_DIRECTORY=\${CUDA_MPS_PIPE_DIRECTORY:-unset}\"
echo \"CUDA_VISIBLE_DEVICES=\${CUDA_VISIBLE_DEVICES:-unset}\"
echo \"SLURM_JOB_GPUS=\${SLURM_JOB_GPUS:-unset}\"
nvidia-smi --query-gpu=name --format=csv,noheader || true"

  mps_jid=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
    bash -c "mkdir -p '${MPS_OUT_DIR}'; cat > /tmp/mps-verify.sbatch <<'EOF_MPS'
${MPS_JOB}
EOF_MPS
sbatch --parsable /tmp/mps-verify.sbatch" 2>/dev/null | tr -d '\r' | tail -n1)

  echo "  Submitted MPS job: ${mps_jid}"
  wait_gpu_pool_responding 150 \
    || warn "${GPU_POOL_STS} did not become Slurm-ready before MPS job polling"

  mps_deadline=$(( $(date +%s) + JOB_TIMEOUT ))
  while true; do
    mps_state=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
      bash -lc "scontrol show job ${mps_jid} 2>/dev/null | grep -oP 'JobState=\K\w+'" \
      2>/dev/null | tr -d '\r' | tail -n1 || echo "")
    printf "  [%s] job %s state=%s\n" "$(date +%H:%M:%S)" "$mps_jid" "${mps_state:-?}"
    case "${mps_state:-}" in
      COMPLETED|FAILED|CANCELLED|TIMEOUT|NODE_FAIL) break ;;
    esac
    if (( $(date +%s) >= mps_deadline )); then
      warn "timed out waiting for MPS job ${mps_jid}"
      cancel_job_quietly "$mps_jid"
      break
    fi
    sleep 4
  done

  mps_out=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
    bash -c "cat '${MPS_OUT_DIR}/${mps_jid}.out' 2>/dev/null || echo ''" \
    2>/dev/null | tr -d '\r')

  echo "  MPS job stdout:"
  echo "$mps_out" | sed 's/^/    /'

  # Without shared NFS, the output file may be on the (already scaled-down)
  # worker pod and unreadable here. In that case fall back to scontrol.
  if [[ -z "$mps_out" && "$mps_state" == "COMPLETED" ]]; then
    mps_info=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
      bash -lc "scontrol show job ${mps_jid} 2>/dev/null" 2>/dev/null | tr -d '\r' || true)
    mps_exit=$(echo "$mps_info" | grep -oP 'ExitCode=\K[^ ]+' || true)
    mps_tres=$(echo "$mps_info" | grep -oP 'TRES=\K[^ ]+' || true)
    echo "  (output file not readable from login — check /shared mount)"
    echo "  ExitCode=${mps_exit}  TRES=${mps_tres}"
    if [[ "$mps_exit" == "0:0" ]]; then
      pass "MPS job ExitCode=0:0"
    else
      warn "MPS job ExitCode='${mps_exit}' — check gres.conf Name=mps and GresTypes"
    fi
  else
    # Slurm sets CUDA_MPS_ACTIVE_THREAD_PERCENTAGE when --gres=mps:N is used.
    if echo "$mps_out" | grep -q "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25"; then
      pass "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25 injected by Slurm prolog"
    elif echo "$mps_out" | grep -q "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=unset"; then
      fail "MPS env not injected — check gres.conf 'Name=mps' and slurm.conf GresTypes"
    else
      warn "CUDA_MPS_ACTIVE_THREAD_PERCENTAGE missing or unexpected in job env"
    fi
  fi

  # grep for an absolute path (starts with /) to avoid matching "=unset".
  if echo "$mps_out" | grep -q "CUDA_MPS_PIPE_DIRECTORY=/"; then
    pass "CUDA_MPS_PIPE_DIRECTORY set (MPS daemon socket reachable)"
  else
    warn "CUDA_MPS_PIPE_DIRECTORY missing or unset — device-plugin sharing.mps may not be active"
  fi
fi

echo ""
echo "=== GPU verification done ==="
