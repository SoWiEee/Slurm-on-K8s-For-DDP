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

find_local_overlay_dir() {
  local repo_dir="$1"
  local candidate

  candidate="$repo_dir/deploy/kubernetes/overlays/stable"
  if [[ -d "$candidate" ]]; then
    echo "$candidate"
    return 0
  fi

  # Windows may materialize symlink as a plain file; try parsing target text.
  if [[ -f "$repo_dir/deploy" && ! -d "$repo_dir/deploy" ]]; then
    local target
    target=$(tr -d '\r\n' < "$repo_dir/deploy" || true)
    target=${target#./}
    if [[ -n "$target" && -d "$repo_dir/$target/kubernetes/overlays/stable" ]]; then
      echo "$repo_dir/$target/kubernetes/overlays/stable"
      return 0
    fi
  fi

  # Last resort: scan repo for overlay directory.
  candidate=$(find "$repo_dir" -type d -path '*/kubernetes/overlays/stable' | head -n 1 || true)
  if [[ -n "$candidate" ]]; then
    echo "$candidate"
    return 0
  fi

  return 1
}

install_nfs_csi_driver() {
  echo "[phase3] installing CSI NFS driver (${NFS_CSI_REF})..."

  # Fast path: remote kustomize install.
  if kubectl apply -k "github.com/kubernetes-csi/csi-driver-nfs/deploy/kubernetes/overlays/stable/?ref=${NFS_CSI_REF}"; then
    return 0
  fi

  echo "[phase3] remote kustomize install failed, fallback to local git clone (Windows symlink-safe path)." >&2
  if ! command -v git >/dev/null 2>&1; then
    echo "git is required for fallback CSI NFS install path." >&2
    exit 1
  fi

  local tmp_dir repo_dir overlay_dir
  tmp_dir=$(mktemp -d)
  repo_dir="${tmp_dir}/csi-driver-nfs"
  trap 'rm -rf "$tmp_dir"' RETURN

  git -c advice.detachedHead=false clone --depth 1 --branch "$NFS_CSI_REF" \
    https://github.com/kubernetes-csi/csi-driver-nfs.git "$repo_dir"

  if ! overlay_dir=$(find_local_overlay_dir "$repo_dir"); then
    echo "[phase3] cannot locate CSI NFS kustomize overlay in cloned repo." >&2
    echo "[phase3] checked default path: $repo_dir/deploy/kubernetes/overlays/stable" >&2
    echo "[phase3] top-level dirs:" >&2
    find "$repo_dir" -maxdepth 3 -type d | sed "s#^$repo_dir#.#" >&2 || true
    exit 1
  fi

  echo "[phase3] applying local overlay: ${overlay_dir}" >&2
  kubectl apply -k "$overlay_dir"
}

if ! kubectl get csidriver nfs.csi.k8s.io >/dev/null 2>&1; then
  install_nfs_csi_driver
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
