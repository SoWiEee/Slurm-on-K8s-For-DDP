#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
PARTITION=${PARTITION:-debug}
SMOKE_DIR=${SMOKE_DIR:-/shared/phase3-smoke}
CPU_STS=${CPU_STS:-slurm-worker-cpu}
GPU_STS=${GPU_STS:-slurm-worker-gpu-a10}

log() { echo "[phaseC] $*"; }

require_context() {
  kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"
  kubectl config use-context "$KUBE_CONTEXT" >/dev/null
}

get_login_pod() {
  kubectl -n "$NAMESPACE" get pod -l app=slurm-login -o jsonpath='{.items[0].metadata.name}'
}

wait_pool_replicas() {
  local sts="$1" want="$2" deadline=$(( $(date +%s) + 300 ))
  while true; do
    local spec ready
    spec="$(kubectl -n "$NAMESPACE" get sts "$sts" -o jsonpath='{.spec.replicas}')"
    ready="$(kubectl -n "$NAMESPACE" get sts "$sts" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
    log "$sts replicas=${spec} ready=${ready}"
    if [[ "${spec}" -ge "${want}" ]] && [[ "${ready}" -ge "${want}" ]]; then
      break
    fi
    if (( $(date +%s) >= deadline )); then
      echo "[phaseC][ERROR] timeout waiting for $sts to reach replicas=${want}" >&2
      kubectl -n "$NAMESPACE" get sts "$sts" -o yaml >&2 || true
      exit 1
    fi
    sleep 5
  done
}

main() {
  require_context

  log "Waiting for operator/login/controller to be ready..."
  kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT" >/dev/null
  kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$ROLLOUT_TIMEOUT" >/dev/null
  kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT" >/dev/null

  log "Resetting pools to baseline..."
  kubectl -n "$NAMESPACE" scale statefulset/"$CPU_STS" --replicas=1 >/dev/null
  kubectl -n "$NAMESPACE" scale statefulset/"$GPU_STS" --replicas=0 >/dev/null
  kubectl -n "$NAMESPACE" rollout status statefulset/"$CPU_STS" --timeout="$ROLLOUT_TIMEOUT" >/dev/null || true

  local login_pod
  login_pod="$(get_login_pod)"
  kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "mkdir -p '${SMOKE_DIR}'"

  local tmp_script
  tmp_script="$(mktemp)"
  cat > "$tmp_script" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
#SBATCH -o /shared/phase3-smoke/out-%j.txt
#SBATCH -e /shared/phase3-smoke/err-%j.txt
echo "jobid=${SLURM_JOB_ID}"
echo "het_size=${SLURM_HET_SIZE:-unknown}"
srun --het-group=0 -N1 -n1 hostname -s | sed 's/^/cpu component from /'
srun --het-group=1 -N1 -n1 hostname -s | sed 's/^/gpu component from /'
EOF

  kubectl -n "$NAMESPACE" cp "$tmp_script" "${login_pod}:/tmp/phaseC-hetero.sbatch"
  rm -f "$tmp_script"
  kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "chmod +x /tmp/phaseC-hetero.sbatch && mv -f /tmp/phaseC-hetero.sbatch '${SMOKE_DIR}/phaseC-hetero.sbatch'"

  log "Submitting heterogeneous CPU+GPU job..."
  local jobid
  jobid="$(kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc     "sbatch --parsable -p '${PARTITION}' -N1 --constraint=cpu : -N1 --constraint=gpu-a10 --gres=gpu:a10:1 '${SMOKE_DIR}/phaseC-hetero.sbatch' | tr -d '\r\n'")"
  log "jobid=${jobid}"

  log "Waiting for pools to scale..."
  wait_pool_replicas "$CPU_STS" 1
  wait_pool_replicas "$GPU_STS" 1

  log "Waiting for job to finish..."
  local deadline=$(( $(date +%s) + 300 ))
  while true; do
    local st
    st="$(kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "squeue -h -j '${jobid}' -o %T 2>/dev/null || true" | tr -d '\r')"
    if [[ -z "$st" ]]; then
      break
    fi
    log "job state=${st}"
    if (( $(date +%s) >= deadline )); then
      echo "[phaseC][ERROR] timeout waiting for heterogeneous job ${jobid}" >&2
      kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "squeue -l || true; scontrol show job '${jobid}' || true" >&2 || true
      exit 1
    fi
    sleep 5
  done

  local out_file="${SMOKE_DIR}/slurm-${jobid}.out"
  local err_file="${SMOKE_DIR}/slurm-${jobid}.err"
  # fallback names
  if ! kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- test -f "$out_file" 2>/dev/null; then
    out_file="${SMOKE_DIR}/out-${jobid}.txt"
    err_file="${SMOKE_DIR}/err-${jobid}.txt"
  fi

  log "Verifying heterogeneous output..."
  kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "test -s '${out_file}' && sed -n '1,200p' '${out_file}'"

  local cpu_hits gpu_hits
  cpu_hits="$(kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "grep -c '^cpu component from slurm-worker-cpu-' '${out_file}' || true" | tr -d '\r')"
  gpu_hits="$(kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "grep -c '^gpu component from slurm-worker-gpu-a10-' '${out_file}' || true" | tr -d '\r')"

  log "cpu_hits=${cpu_hits} gpu_hits=${gpu_hits}"
  if [[ "${cpu_hits}" -lt 1 || "${gpu_hits}" -lt 1 ]]; then
    echo "[phaseC][ERROR] heterogeneous job did not land on both CPU and GPU pools" >&2
    kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "sed -n '1,240p' '${out_file}' || true; sed -n '1,240p' '${err_file}' || true" >&2 || true
    exit 1
  fi

  echo "Phase C verification passed: one heterogeneous Slurm job used both CPU and GPU NodeSets."
}

main "$@"
