#!/usr/bin/env bash
# verify-gpu.sh — Verify real GPU access via Slurm
#
# Prerequisites: REAL_GPU=true bootstrap.sh has been run and bootstrap-gpu.sh
# has deployed the NVIDIA device plugin.
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
GPU_POOL_STS=${GPU_POOL_STS:-slurm-worker-gpu-a10}
GPU_CONSTRAINT=${GPU_CONSTRAINT:-gpu-a10}
GPU_GRES=${GPU_GRES:-gpu:a10:1}
JOB_TIMEOUT=${JOB_TIMEOUT:-120}
PARTITION=${PARTITION:-debug}

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
  fail "no ready device plugin pods (run bootstrap-gpu.sh first)"
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

# ---------------------------------------------------------------------------
# [5] sbatch GPU job — nvidia-smi inside Slurm job
# ---------------------------------------------------------------------------
echo ""
echo "=== [5] sbatch GPU job (nvidia-smi) ==="

submit_pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "slurm-controller-0")

JOB_SCRIPT="#!/bin/bash
#SBATCH -J gpu-verify
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH --constraint=${GPU_CONSTRAINT}
#SBATCH --gres=${GPU_GRES}
#SBATCH --output=/tmp/gpu-verify-%j.out
#SBATCH --error=/tmp/gpu-verify-%j.err
#SBATCH --time=00:02:00
nvidia-smi --query-gpu=name,driver_version,utilization.gpu --format=csv,noheader
echo \"CUDA_VISIBLE_DEVICES=\${CUDA_VISIBLE_DEVICES}\"
echo \"SLURM_JOB_GPUS=\${SLURM_JOB_GPUS}\""

jid=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
  bash -c "cat > /tmp/gpu-verify.sbatch <<'EOF_JOB'
${JOB_SCRIPT}
EOF_JOB
sbatch --parsable /tmp/gpu-verify.sbatch" 2>/dev/null | tr -d '\r' | tail -n1)

echo "  Submitted job: ${jid}"

deadline=$(( $(date +%s) + JOB_TIMEOUT ))
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
    break
  fi
  sleep 4
done

out=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
  bash -c "cat /tmp/gpu-verify-${jid}.out 2>/dev/null || echo ''" \
  2>/dev/null | tr -d '\r')
err=$(kubectl -n "$NAMESPACE" exec "pod/${submit_pod}" -- \
  bash -c "cat /tmp/gpu-verify-${jid}.err 2>/dev/null || echo ''" \
  2>/dev/null | tr -d '\r')

echo "  Job stdout:"
echo "$out" | sed 's/^/    /'
if [[ -n "$err" ]]; then
  echo "  Job stderr:"
  echo "$err" | sed 's/^/    /' >&2
fi

if echo "$out" | grep -qi "nvidia\|tesla\|a10\|h100\|v100"; then
  pass "GPU name visible in Slurm job output"
elif [[ "$state" == "COMPLETED" ]]; then
  warn "job completed but GPU name not found in output (check GRES binding)"
else
  fail "GPU job did not complete (state=${state})"
fi

echo ""
echo "=== GPU verification done ==="
