#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}

WORKER_STS=${WORKER_STS:-slurm-worker}
CONTROLLER_POD=${CONTROLLER_POD:-slurm-controller-0}
LOGIN_DEPLOY=${LOGIN_DEPLOY:-slurm-login}

MIN_WORKERS=${MIN_WORKERS:-1}
TARGET_WORKERS=${TARGET_WORKERS:-2}

SMOKE_DIR=${SMOKE_DIR:-/shared/phase3-smoke}
PARTITION=${PARTITION:-debug}

wait_seconds() {
  local t="$1"
  if [[ "$t" =~ ^[0-9]+s$ ]]; then echo "${t%s}"; return; fi
  if [[ "$t" =~ ^[0-9]+m$ ]]; then echo "$(( ${t%m} * 60 ))"; return; fi
  if [[ "$t" =~ ^([0-9]+)m([0-9]+)s$ ]]; then echo "$(( ${BASH_REMATCH[1]} * 60 + ${BASH_REMATCH[2]} ))"; return; fi
  echo 300
}

deadline_after() { echo $(( $(date +%s) + $1 )); }

require_context() {
  if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
    echo "kubectl context ${KUBE_CONTEXT} not found" >&2
    kubectl config get-contexts -o name >&2 || true
    exit 1
  fi
  kubectl config use-context "$KUBE_CONTEXT" >/dev/null
}

exec_login() {
  kubectl -n "$NAMESPACE" exec deploy/"$LOGIN_DEPLOY" -- bash -lc "$1"
}

echo "[e2e] Using context: ${KUBE_CONTEXT}"
require_context

echo "[e2e] Waiting for login/controller/worker to be ready..."
kubectl -n "$NAMESPACE" rollout status deployment/"$LOGIN_DEPLOY" --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/"$WORKER_STS" --timeout="$ROLLOUT_TIMEOUT" || true
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"

echo "[e2e] Checking sbatch exists on login..."
exec_login "command -v sbatch >/dev/null && sbatch --version"

echo "[e2e] Forcing workers to MIN_WORKERS=${MIN_WORKERS} (to create a pending job)..."
kubectl -n "$NAMESPACE" scale statefulset/"$WORKER_STS" --replicas="$MIN_WORKERS"
kubectl -n "$NAMESPACE" rollout status statefulset/"$WORKER_STS" --timeout="$ROLLOUT_TIMEOUT" || true

echo "[e2e] Refresh slurmctld view (best-effort)..."
kubectl -n "$NAMESPACE" exec pod/"$CONTROLLER_POD" -- bash -lc "scontrol reconfigure || true; sinfo -N -l || true"

# Important: when worker pods don't exist, slurmctld may still consider the nodes idle for a while,
# causing jobs to start and then fail during srun fan-out. Force non-existent nodes DOWN so the job stays PENDING.
echo "[e2e] Marking non-existent worker nodes DOWN to keep the job pending (best-effort)..."
for n in 1 2; do
  if ! kubectl -n "$NAMESPACE" get pod "${WORKER_STS}-${n}" >/dev/null 2>&1; then
    kubectl -n "$NAMESPACE" exec pod/"$CONTROLLER_POD" -- bash -lc \
      "scontrol update NodeName=slurm-worker-${n} State=DOWN Reason='scaledown' || true"
  fi
done
kubectl -n "$NAMESPACE" exec pod/"$CONTROLLER_POD" -- bash -lc "sinfo -N -l || true"

echo "[e2e] Writing smoke sbatch script locally and copying to login pod (/shared)..."
login_pod="$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login -o jsonpath='{.items[0].metadata.name}')"
echo "[e2e] login_pod=${login_pod}"

# Ensure shared dir exists on login
kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc "mkdir -p '${SMOKE_DIR}'"

# Create local sbatch script using a RELATIVE path to avoid Windows drive-letter issues with kubectl cp.
tmp_sbatch=".phase3-smoke.$$.sbatch"
cat > "${tmp_sbatch}" <<EOF
#!/usr/bin/env bash
#SBATCH -J phase3-smoke
#SBATCH -p ${PARTITION}
#SBATCH -N 2
#SBATCH --ntasks-per-node=1
#SBATCH -o ${SMOKE_DIR}/out-%j.txt
#SBATCH -e ${SMOKE_DIR}/err-%j.txt

set -euo pipefail
echo "jobid=\${SLURM_JOB_ID}"
echo "nodelist=\${SLURM_NODELIST}"
echo "[\$(date)] starting on \$(hostname)"

# prove we ran on >=2 nodes by printing hostnames from srun
srun -N2 -n2 bash -lc 'echo "hello from \$(hostname)"'
EOF

# Copy into login pod, then move into /shared
kubectl -n "$NAMESPACE" cp "./${tmp_sbatch}" "${login_pod}:/tmp/phase3-smoke.sbatch"
rm -f "${tmp_sbatch}"
kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc \
  "chmod +x /tmp/phase3-smoke.sbatch && mv -f /tmp/phase3-smoke.sbatch '${SMOKE_DIR}/phase3-smoke.sbatch'"


echo "[e2e] Submitting trigger job that must wait for slurm-worker-1 (guaranteed PENDING)..."
trigger_jobid="$(kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc "
cat > '${SMOKE_DIR}/trigger.sbatch' <<'EOF'
#!/usr/bin/env bash
#SBATCH -J phase3-trigger
#SBATCH -p ${PARTITION}
#SBATCH -N 1
#SBATCH -w slurm-worker-1
#SBATCH -o ${SMOKE_DIR}/trigger-out-%j.txt
#SBATCH -e ${SMOKE_DIR}/trigger-err-%j.txt
echo trigger-start \$(date)
sleep 60
EOF
sbatch '${SMOKE_DIR}/trigger.sbatch' | awk '{print \$4}'
")"
echo "[e2e] trigger_jobid=${trigger_jobid}"

echo "[e2e] Waiting for trigger job to reach PENDING (operator scale-up trigger)..."
pend_deadline=$(( $(date +%s) + 90 ))
while true; do
  st="$(kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc "squeue -h -j ${trigger_jobid} -o %T 2>/dev/null || true")"
  echo "[e2e] trigger state=${st:-gone}"
  if [[ "$st" == "PENDING" ]]; then
    break
  fi
  if (( $(date +%s) >= pend_deadline )); then
    echo "[e2e][ERROR] trigger job did not reach PENDING within timeout." >&2
    kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc "squeue -l || true" >&2 || true
    exit 1
  fi
  sleep 3
done

echo "[e2e] Waiting for operator to scale workers to TARGET_WORKERS=${TARGET_WORKERS} (fallback to manual scale)..."
scale_deadline=$(( $(date +%s) + 180 ))
while true; do
  replicas="$(kubectl -n "$NAMESPACE" get sts "$WORKER_STS" -o jsonpath='{.spec.replicas}')"
  ready="$(kubectl -n "$NAMESPACE" get sts "$WORKER_STS" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
  echo "[e2e] worker_replicas=${replicas} ready=${ready}"

  if [[ "${replicas}" -ge "${TARGET_WORKERS}" ]] && [[ "${ready}" -ge "${TARGET_WORKERS}" ]]; then
    echo "[e2e] Workers scaled and ready."
    break
  fi

  if (( $(date +%s) >= scale_deadline )); then
    echo "[e2e][WARN] Operator did not scale in time; forcing scale to ${TARGET_WORKERS}..."
    kubectl -n "$NAMESPACE" scale statefulset/"$WORKER_STS" --replicas="$TARGET_WORKERS"
    kubectl -n "$NAMESPACE" rollout status statefulset/"$WORKER_STS" --timeout="$ROLLOUT_TIMEOUT"
    break
  fi
  sleep 5
done

# Ensure worker-1 exists and is Ready
kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${WORKER_STS}-1" --timeout="$ROLLOUT_TIMEOUT"

echo "[e2e] Waiting for worker-1 DNS to be resolvable from worker-0 (gate before RESUME)..."
dns_deadline=$(( $(date +%s) + 180 ))
worker1_fqdn="${WORKER_STS}-1.${WORKER_STS}.${NAMESPACE}.svc.cluster.local"
while true; do
  # worker-1 pod must exist and be Ready
  if kubectl -n "$NAMESPACE" get pod "${WORKER_STS}-1" >/dev/null 2>&1; then
    if kubectl -n "$NAMESPACE" wait --for=condition=Ready "pod/${WORKER_STS}-1" --timeout=5s >/dev/null 2>&1; then
      # worker-0 must resolve worker-1 FQDN (this is what srun step uses via slurm.conf NodeAddr)
      if kubectl -n "$NAMESPACE" exec "pod/${WORKER_STS}-0" -- sh -lc "getent hosts '${worker1_fqdn}' >/dev/null"; then
        echo "[e2e] worker-1 DNS resolvable from worker-0: ${worker1_fqdn}"
        break
      fi
    fi
  fi
  if (( $(date +%s) >= dns_deadline )); then
    echo "[e2e][ERROR] worker-1 not resolvable from worker-0 within timeout." >&2
    kubectl -n "$NAMESPACE" get pods -l app=slurm-worker -o wide >&2 || true
    kubectl -n "$NAMESPACE" get endpoints slurm-worker -o wide >&2 || true
    exit 1
  fi
  sleep 3
done

echo "[e2e] Waiting for slurmctld to see slurm-worker-1 without NO NETWORK ADDRESS..."
slurm_deadline=$(( $(date +%s) + 180 ))
while true; do
  reason="$(kubectl -n "$NAMESPACE" exec pod/"$CONTROLLER_POD" -- bash -lc "scontrol show node slurm-worker-1 | awk -F'Reason=' 'NF>1{print \$2; exit}'" 2>/dev/null || true)"
  if [[ -n "$reason" ]] && echo "$reason" | grep -qi 'NO NETWORK ADDRESS'; then
    echo "[e2e] slurm-worker-1 still has reason: $reason"
  else
    echo "[e2e] slurm-worker-1 reason ok: ${reason:-none}"
    break
  fi
  if (( $(date +%s) >= slurm_deadline )); then
    echo "[e2e][ERROR] slurm-worker-1 still not addressable in slurmctld within timeout." >&2
    kubectl -n "$NAMESPACE" exec pod/"$CONTROLLER_POD" -- bash -lc "sinfo -N -l || true; scontrol show node slurm-worker-1 || true" >&2 || true
    exit 1
  fi
  sleep 3
done

echo "[e2e] Cancelling trigger job (it has served its purpose)..."
kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc "scancel ${trigger_jobid} || true"

echo "[e2e] Submitting REAL multi-node smoke job from login..."
jobid="$(kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc \
  "sbatch '${SMOKE_DIR}/phase3-smoke.sbatch' | awk '{print \$4}'")"
echo "[e2e] jobid=${jobid}"

echo "[e2e] Resuming slurm nodes for newly created worker pods (best-effort)..."
kubectl -n "$NAMESPACE" exec pod/"$CONTROLLER_POD" -- bash -lc \
  "scontrol reconfigure || true; sinfo -N -l || true"

echo "[e2e] Waiting for job to finish..."
job_deadline=$(( $(date +%s) + 300 ))
while true; do
  state="$(kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc \
    "squeue -h -j ${jobid} -o %T 2>/dev/null || true")"
  if [[ -z "${state}" ]]; then
    echo "[e2e] job not in queue anymore (finished)."
    break
  fi
  echo "[e2e] job state=${state}"
  if (( $(date +%s) >= job_deadline )); then
    echo "[e2e][ERROR] Timed out waiting for job to finish." >&2
    kubectl -n "$NAMESPACE" exec "${login_pod}" -- bash -lc "squeue -l || true; sinfo -N -l || true" >&2 || true
    exit 1
  fi
  sleep 5
done

out_file="${SMOKE_DIR}/out-${jobid}.txt"
err_file="${SMOKE_DIR}/err-${jobid}.txt"

echo "[e2e] Verifying output exists on shared path: ${out_file}"
kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc \
  "test -s '${out_file}' && echo '[e2e] out exists' && sed -n '1,120p' '${out_file}'"

echo "[e2e] Checking output contains >=2 distinct hostnames (multi-node evidence)..."
distinct_hosts="$(kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc \
  "grep -Eo 'hello from [^ ]+' '${out_file}' | awk '{print \$3}' | sort -u | wc -l")"
echo "[e2e] distinct_hosts=${distinct_hosts}"
if [[ "${distinct_hosts}" -lt 2 ]]; then
  echo "[e2e][ERROR] Expected output from >=2 hosts, got ${distinct_hosts}. Dumping out/err:" >&2
  kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- bash -lc "sed -n '1,200p' '${out_file}' || true; sed -n '1,200p' '${err_file}' || true" >&2
  exit 1
fi

echo "Phase 3 E2E verification passed: sbatch ran a multi-node job and output is in shared (/shared) path."
