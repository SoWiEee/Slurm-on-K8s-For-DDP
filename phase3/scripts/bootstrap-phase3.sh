#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
NFS_CSI_REF=${NFS_CSI_REF:-v4.9.0}
ENABLE_CHECKPOINT_GUARD=${ENABLE_CHECKPOINT_GUARD:-true}
CHECKPOINT_PATH=${CHECKPOINT_PATH:-/shared/checkpoints/latest.ckpt}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller slurm-worker >/dev/null 2>&1; then
  echo "Phase 1 resources not found in namespace ${NAMESPACE}." >&2
  exit 1
fi

if ! kubectl -n "$NAMESPACE" get deployment slurm-elastic-operator >/dev/null 2>&1; then
  echo "Phase 2 operator not found. Run phase2/scripts/bootstrap-phase2.sh first." >&2
  exit 1
fi

if ! kubectl get csidriver nfs.csi.k8s.io >/dev/null 2>&1; then
  echo "[phase3] installing CSI NFS driver (${NFS_CSI_REF})..."
  kubectl apply -k "github.com/kubernetes-csi/csi-driver-nfs/deploy/kubernetes/overlays/stable/?ref=${NFS_CSI_REF}"
fi

kubectl apply -f phase3/manifests/nfs-shared-storage.yaml
kubectl -n "$NAMESPACE" rollout status deployment/slurm-nfs --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Bound pvc/slurm-shared-pvc --timeout=120s

controller_patch=$(cat <<'JSON'
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "slurm-controller",
            "volumeMounts": [
              {
                "name": "shared-storage",
                "mountPath": "/shared"
              }
            ]
          }
        ],
        "volumes": [
          {
            "name": "shared-storage",
            "persistentVolumeClaim": {
              "claimName": "slurm-shared-pvc"
            }
          }
        ]
      }
    }
  }
}
JSON
)

worker_patch=$(cat <<'JSON'
{
  "spec": {
    "template": {
      "spec": {
        "containers": [
          {
            "name": "slurm-worker",
            "volumeMounts": [
              {
                "name": "shared-storage",
                "mountPath": "/shared"
              }
            ]
          }
        ],
        "volumes": [
          {
            "name": "shared-storage",
            "persistentVolumeClaim": {
              "claimName": "slurm-shared-pvc"
            }
          }
        ]
      }
    }
  }
}
JSON
)

kubectl -n "$NAMESPACE" patch statefulset slurm-controller --type=merge -p "$controller_patch"
kubectl -n "$NAMESPACE" patch statefulset slurm-worker --type=merge -p "$worker_patch"

kubectl -n "$NAMESPACE" set env deployment/slurm-elastic-operator \
  CHECKPOINT_GUARD_ENABLED="${ENABLE_CHECKPOINT_GUARD}" \
  CHECKPOINT_PATH="${CHECKPOINT_PATH}" \
  MAX_CHECKPOINT_AGE_SECONDS=600 >/dev/null

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$ROLLOUT_TIMEOUT"

echo "Phase 3 shared storage setup completed."
