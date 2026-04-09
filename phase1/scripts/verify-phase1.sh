#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null
kubectl -n "$NAMESPACE" get pods -o wide
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-controller-0 --timeout=120s
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-cpu-0 --timeout=120s

# Wait for slurmctld to accept connections (pod Ready != daemon ready).
echo "waiting for slurmctld to be ready..."
for _ in $(seq 1 30); do
  if kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- scontrol ping >/dev/null 2>&1; then
    break
  fi
  sleep 3
done
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- scontrol ping

kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sinfo
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- scontrol show nodes
# SSH inter-pod connectivity is not verified here (munge auth is sufficient for Slurm operations)

# Verify StateSaveLocation PVC is bound.
pvc_phase=$(kubectl -n "$NAMESPACE" get pvc slurm-ctld-state -o jsonpath='{.status.phase}' 2>/dev/null || true)
if [[ "$pvc_phase" == "Bound" ]]; then
  echo "slurm-ctld-state PVC: Bound"
else
  echo "WARNING: slurm-ctld-state PVC not bound (phase=${pvc_phase:-missing})" >&2
fi

# Verify accounting stack if deployed.
if kubectl -n "$NAMESPACE" get deployment slurmdbd >/dev/null 2>&1; then
  kubectl -n "$NAMESPACE" wait --for=condition=Ready pod -l app=mysql --timeout=60s || \
    echo "WARNING: MySQL pod not ready" >&2
  kubectl -n "$NAMESPACE" wait --for=condition=Available deployment/slurmdbd --timeout=60s || \
    echo "WARNING: slurmdbd deployment not available" >&2
  # Verify slurmctld can reach slurmdbd.
  sacct_out=$(kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- \
    bash -lc 'sacct --noheader -X 2>&1 | head -3 || true')
  echo "sacct check: ${sacct_out}"
fi
