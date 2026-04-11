#!/usr/bin/env bash
# bootstrap-phase5.sh — build Lmod-enabled images, apply modulefiles, restart pods
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
NAMESPACE=${NAMESPACE:-slurm}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found — run bootstrap-dev.sh first" >&2
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

# ---------------------------------------------------------------------------
# 1. Build images (worker + controller now include lmod)
# ---------------------------------------------------------------------------
echo "[phase5] building slurm-controller:phase1 (with lmod)..."
docker build -t slurm-controller:phase1 phase1/docker/controller/

echo "[phase5] building slurm-worker:phase1 (with lmod + openmpi)..."
docker build -t slurm-worker:phase1 phase1/docker/worker/

echo "[phase5] loading images into Kind cluster ${CLUSTER_NAME}..."
kind load docker-image slurm-controller:phase1 --name "$CLUSTER_NAME"
kind load docker-image slurm-worker:phase1     --name "$CLUSTER_NAME"

# ---------------------------------------------------------------------------
# 2. Apply modulefile ConfigMaps
# ---------------------------------------------------------------------------
echo "[phase5] applying lmod modulefile ConfigMaps..."
kubectl apply -f phase5/manifests/lmod-modulefiles.yaml

# ---------------------------------------------------------------------------
# 3. Re-render slurm-static.yaml with --with-lmod --with-shared-storage
#    (Phase 5 requires Phase 3 NFS to be deployed for shared job output paths)
# ---------------------------------------------------------------------------
echo "[phase5] re-rendering slurm-static.yaml with --with-lmod --with-shared-storage..."
if py -3 phase1/scripts/render-slurm-static.py --with-lmod --with-shared-storage 2>/dev/null; then
  true
else
  python3 phase1/scripts/render-slurm-static.py --with-lmod --with-shared-storage
fi
kubectl apply -f phase1/manifests/slurm-static.yaml

# ---------------------------------------------------------------------------
# 4. Rolling restart so pods pick up the new image + volume mounts
# ---------------------------------------------------------------------------
echo "[phase5] restarting pods..."
for r in statefulset/slurm-controller statefulset/slurm-worker-cpu deployment/slurm-login; do
  kubectl -n "$NAMESPACE" rollout restart "$r" >/dev/null 2>&1 || true
done

# Force delete so StatefulSet pods come up immediately.
kubectl -n "$NAMESPACE" delete pod -l app=slurm-controller --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-cpu --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-login       --ignore-not-found >/dev/null 2>&1 || true

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker-cpu --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login        --timeout="$ROLLOUT_TIMEOUT"

# ---------------------------------------------------------------------------
# 5. Wait for slurmctld
# ---------------------------------------------------------------------------
echo "[phase5] waiting for slurmctld..."
for _ in $(seq 1 60); do
  if kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- bash -lc 'scontrol ping >/dev/null 2>&1'; then
    break
  fi
  sleep 3
done

# ---------------------------------------------------------------------------
# 6. Ensure shared job output directory exists on NFS
# ---------------------------------------------------------------------------
echo "[phase5] ensuring /shared/jobs directory exists..."
# Wait for the controller pod to be ready before exec-ing into it.
# The rolling restart above may still be in progress; without this wait the
# exec silently fails (swallowed by || true) and /shared/jobs is never created.
kubectl -n "$NAMESPACE" wait pod/slurm-controller-0 --for=condition=Ready --timeout=120s >/dev/null
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- mkdir -p /shared/jobs || true

echo ""
echo "Phase 5 bootstrap complete.  Run: bash phase5/scripts/verify-phase5.sh"
