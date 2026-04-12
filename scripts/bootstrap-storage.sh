#!/usr/bin/env bash
set -euo pipefail

# Resolve a working Python 3 interpreter.
# Override with PYTHON=/path/to/python3 if auto-detection fails.
if [[ -z "${PYTHON:-}" ]]; then
  for _py in python3 python py; do
    if command -v "$_py" >/dev/null 2>&1 && "$_py" -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" 2>/dev/null; then
      PYTHON="$_py"; break
    fi
  done
  : "${PYTHON:?Cannot find a working Python 3.8+ interpreter. Set PYTHON=/path/to/python3}"
fi

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
NFS_SERVER=${NFS_SERVER:-}
NFS_PATH=${NFS_PATH:-/srv/nfs/k8s}
PROVISIONER_NAMESPACE=${PROVISIONER_NAMESPACE:-nfs-provisioner}

if [[ -z "$NFS_SERVER" ]]; then
  cat >&2 <<USAGE
NFS_SERVER is required.

Examples:
  # If your Kind/Docker can resolve it (often works on Docker Desktop):
  NFS_SERVER=host.docker.internal NFS_PATH=/srv/nfs/k8s bash scripts/bootstrap-storage.sh

  # Or use an IP reachable from Kind containers:
  NFS_SERVER=192.168.x.y NFS_PATH=/srv/nfs/k8s bash scripts/bootstrap-storage.sh
USAGE
  exit 1
fi

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

if ! kubectl get ns "$NAMESPACE" >/dev/null 2>&1; then
  echo "Namespace ${NAMESPACE} not found. Run Phase 1 bootstrap first." >&2
  exit 1
fi

if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller slurm-worker-cpu >/dev/null 2>&1; then
  echo "Cluster resources not found in namespace ${NAMESPACE}. run scripts/bootstrap.sh first." >&2
  exit 1
fi

rendered_manifest=$(mktemp)
trap 'rm -f "$rendered_manifest"' EXIT

sed -e "s|__NFS_SERVER__|${NFS_SERVER}|g"     -e "s|__NFS_PATH__|${NFS_PATH}|g"     manifests/storage/nfs-subdir-provisioner.tmpl.yaml > "$rendered_manifest"

kubectl apply -f "$rendered_manifest"
kubectl -n "$PROVISIONER_NAMESPACE" rollout status deployment/nfs-subdir-external-provisioner --timeout="$ROLLOUT_TIMEOUT"

kubectl apply -f manifests/storage/shared-storage.yaml

# NOTE: PVC "Bound" is a phase (status.phase), not a Condition, so `kubectl wait --for=condition=Bound pvc/...`
# can time out even when the PVC is already Bound. Use a polling loop on .status.phase instead.
echo "Waiting for PVC slurm-shared-rwx to become Bound..."
wait_seconds() {
  local t="$1"
  # supports 300s / 5m / 2m30s (best-effort)
  if [[ "$t" =~ ^[0-9]+s$ ]]; then echo "${t%s}"; return; fi
  if [[ "$t" =~ ^[0-9]+m$ ]]; then echo "$(( ${t%m} * 60 ))"; return; fi
  if [[ "$t" =~ ^([0-9]+)m([0-9]+)s$ ]]; then echo "$(( ${BASH_REMATCH[1]} * 60 + ${BASH_REMATCH[2]} ))"; return; fi
  # fallback
  echo 300
}
deadline=$(( $(date +%s) + $(wait_seconds "$ROLLOUT_TIMEOUT") ))
while true; do
  phase="$(kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [[ "$phase" == "Bound" ]]; then
    echo "PVC slurm-shared-rwx is Bound."
    break
  fi
  if (( $(date +%s) >= deadline )); then
    echo "[ERROR] Timed out waiting for PVC slurm-shared-rwx to become Bound." >&2
    kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx -o yaml >&2 || true
    kubectl -n "$NAMESPACE" describe pvc slurm-shared-rwx >&2 || true
    echo "Provisioner diagnostics:" >&2
    kubectl -n "$PROVISIONER_NAMESPACE" get pods -o wide >&2 || true
    kubectl -n "$PROVISIONER_NAMESPACE" logs deployment/nfs-subdir-external-provisioner --tail=200 >&2 || true
    exit 1
  fi
  sleep 2
done

# Regenerate slurm-static.yaml with the /shared NFS volume baked in, then apply it.
# This is persistent: subsequent bootstrap.sh re-runs will detect the PVC and call
# render-core.py --with-shared-storage automatically, so the NFS mount survives any
# future kubectl apply of slurm-static.yaml.
echo "Regenerating slurm-static.yaml with --with-lmod --with-shared-storage..."
"$PYTHON" scripts/render-core.py --with-lmod --with-shared-storage
kubectl apply -f manifests/core/slurm-static.yaml

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker-cpu --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT"

echo "Storage deployment completed."
echo "NFS_SERVER=${NFS_SERVER}, NFS_PATH=${NFS_PATH}"
