#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
PARTITION=${PARTITION:-debug}
VERIFY_TIMEOUT_SECONDS=${VERIFY_TIMEOUT_SECONDS:-240}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-controller-0 --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Available deployment/slurm-elastic-operator --timeout=120s
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Bound pvc/slurm-shared-pvc --timeout=120s

# Start from 1 worker to verify auto scale-up.
kubectl -n "$NAMESPACE" scale statefulset/slurm-worker --replicas=1
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout=120s

job_id=$(kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "cat <<'EOF' >/tmp/phase3-mpi-job.sh
#!/bin/bash
set -euo pipefail
mkdir -p /shared/jobs /shared/checkpoints
srun -N 2 -n 4 -p ${PARTITION} bash -lc 'echo \"job=$SLURM_JOB_ID rank=${SLURM_PROCID} host=$(hostname)\" | tee -a /shared/jobs/mpi-hosts-${SLURM_JOB_ID}.txt'
srun -N 1 -n 1 -p ${PARTITION} bash -lc 'date -u +%FT%TZ > /shared/checkpoints/latest.ckpt'
sleep 90
EOF
sbatch --parsable -N 2 -n 4 -p ${PARTITION} /tmp/phase3-mpi-job.sh")

echo "submitted MPI-style job: ${job_id}"

start_ts=$(date +%s)
scaled_up=false
while true; do
  replicas=$(kubectl -n "$NAMESPACE" get statefulset/slurm-worker -o jsonpath='{.spec.replicas}')
  if [[ "$replicas" -ge 2 ]]; then
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
  echo "operator did not scale up for MPI-style job" >&2
  kubectl -n "$NAMESPACE" logs deployment/slurm-elastic-operator --tail=200 >&2 || true
  exit 1
fi

echo "scale-up verified: replicas=$(kubectl -n "$NAMESPACE" get statefulset/slurm-worker -o jsonpath='{.spec.replicas}')"

# Verify shared storage is writable/readable from worker and controller.
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "test -s /shared/checkpoints/latest.ckpt"
kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- bash -lc "test -s /shared/checkpoints/latest.ckpt"
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc "ls -l /shared/jobs/mpi-hosts-${job_id}.txt"

echo "shared storage verified across controller/worker"

kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- scancel "$job_id" || true
sleep 95

replicas=$(kubectl -n "$NAMESPACE" get statefulset/slurm-worker -o jsonpath='{.spec.replicas}')
if [[ "$replicas" -gt 1 ]]; then
  echo "operator did not scale down idle workers (replicas=${replicas})" >&2
  kubectl -n "$NAMESPACE" logs deployment/slurm-elastic-operator --tail=200 >&2 || true
  exit 1
fi

echo "scale-down verified: replicas=${replicas}"
