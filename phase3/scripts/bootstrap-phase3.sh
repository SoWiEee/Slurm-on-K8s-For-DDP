#!/usr/bin/env bash
set -euo pipefail

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
Example:
  NFS_SERVER=192.168.65.2 NFS_PATH=/srv/nfs/k8s bash phase3/scripts/bootstrap-phase3.sh
USAGE
  exit 1
fi

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller slurm-worker >/dev/null 2>&1; then
  echo "Phase 1 resources not found in namespace ${NAMESPACE}. run scripts/bootstrap-dev.sh first." >&2
  exit 1
fi

rendered_manifest=$(mktemp)
trap 'rm -f "$rendered_manifest"' EXIT

sed -e "s|__NFS_SERVER__|${NFS_SERVER}|g" \
    -e "s|__NFS_PATH__|${NFS_PATH}|g" \
    phase3/manifests/nfs-subdir-provisioner.tmpl.yaml > "$rendered_manifest"

kubectl apply -f "$rendered_manifest"
kubectl -n "$PROVISIONER_NAMESPACE" rollout status deployment/nfs-subdir-external-provisioner --timeout="$ROLLOUT_TIMEOUT"

kubectl apply -f phase3/manifests/shared-storage.yaml
kubectl -n "$NAMESPACE" wait --for=condition=Bound pvc/slurm-shared-rwx --timeout="$ROLLOUT_TIMEOUT"

patch='{"spec":{"template":{"spec":{"volumes":[{"name":"shared-storage","persistentVolumeClaim":{"claimName":"slurm-shared-rwx"}}],"containers":[{"name":"slurm-controller","volumeMounts":[{"name":"shared-storage","mountPath":"/shared"}]}]}}}}'
kubectl -n "$NAMESPACE" patch statefulset slurm-controller --type strategic -p "$patch"

patch='{"spec":{"template":{"spec":{"volumes":[{"name":"shared-storage","persistentVolumeClaim":{"claimName":"slurm-shared-rwx"}}],"containers":[{"name":"slurm-worker","volumeMounts":[{"name":"shared-storage","mountPath":"/shared"}]}]}}}}'
kubectl -n "$NAMESPACE" patch statefulset slurm-worker --type strategic -p "$patch"

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT"

echo "Phase 3 storage deployment completed."
echo "NFS_SERVER=${NFS_SERVER}, NFS_PATH=${NFS_PATH}"
