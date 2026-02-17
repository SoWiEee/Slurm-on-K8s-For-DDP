#!/usr/bin/env bash
set -euo pipefail


jsonpatch_escape() {
  # Usage: jsonpatch_escape 'string' -> prints JSON-escaped string
  python3 - <<'PY' "$1"
import json,sys
print(json.dumps(sys.argv[1]))
PY
}

patch_add_shared_volume() {
  local kind="$1" name="$2" ns="$3"
  # Add volume if not present
  if ! kubectl -n "$ns" get "$kind" "$name" -o jsonpath='{.spec.template.spec.volumes[*].name}' | tr ' ' '
' | grep -qx 'shared-storage'; then
    kubectl -n "$ns" patch "$kind" "$name" --type='json' -p='[{"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"shared-storage","persistentVolumeClaim":{"claimName":"slurm-shared-rwx"}}}]'
  fi
}

patch_add_shared_mount() {
  local kind="$1" name="$2" ns="$3" container="$4" mountPath="$5"
  # Add mount if not present
  if ! kubectl -n "$ns" get "$kind" "$name" -o jsonpath='{.spec.template.spec.containers[?(@.name=="'"$container"'")].volumeMounts[*].mountPath}' 2>/dev/null | tr ' ' '
' | grep -qx "$mountPath"; then
    kubectl -n "$ns" patch "$kind" "$name" --type='json' -p='[{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"shared-storage","mountPath":"'"$mountPath"'"}}]'
  fi
}

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
  NFS_SERVER=host.docker.internal NFS_PATH=/srv/nfs/k8s bash phase3/scripts/bootstrap-phase3.sh

  # Or use an IP reachable from Kind containers:
  NFS_SERVER=192.168.x.y NFS_PATH=/srv/nfs/k8s bash phase3/scripts/bootstrap-phase3.sh
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

if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller slurm-worker >/dev/null 2>&1; then
  echo "Phase 1 resources not found in namespace ${NAMESPACE}. run scripts/bootstrap-dev.sh first." >&2
  exit 1
fi

rendered_manifest=$(mktemp)
trap 'rm -f "$rendered_manifest"' EXIT

sed -e "s|__NFS_SERVER__|${NFS_SERVER}|g"     -e "s|__NFS_PATH__|${NFS_PATH}|g"     phase3/manifests/nfs-subdir-provisioner.tmpl.yaml > "$rendered_manifest"

kubectl apply -f "$rendered_manifest"
kubectl -n "$PROVISIONER_NAMESPACE" rollout status deployment/nfs-subdir-external-provisioner --timeout="$ROLLOUT_TIMEOUT"

kubectl apply -f phase3/manifests/shared-storage.yaml

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

# Helper: ensure a volume + volumeMount exist (idempotent) using JSONPatch.
ensure_mount() {
  local kind="$1" name="$2" container="$3" vol_name="$4" claim="$5" mnt="$6"

  # Add volume if missing
  if ! kubectl -n "$NAMESPACE" get "$kind/$name" \
        -o jsonpath='{range .spec.template.spec.volumes[*]}{.name}{"\n"}{end}' \
        2>/dev/null | grep -qx "$vol_name"; then
    local vol_patch
    vol_patch=$(printf '[{"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"%s","persistentVolumeClaim":{"claimName":"%s"}}}]' \
      "$vol_name" "$claim")
    kubectl -n "$NAMESPACE" patch "$kind/$name" --type='json' -p "$vol_patch"
  fi

  # Find container index (kubectl jsonpath does NOT support $i,$c := ...)
  local names idx=-1 i=0
  names="$(kubectl -n "$NAMESPACE" get "$kind/$name" -o jsonpath='{.spec.template.spec.containers[*].name}')"
  for n in $names; do
    if [[ "$n" == "$container" ]]; then idx=$i; break; fi
    i=$((i+1))
  done
  if (( idx < 0 )); then
    echo "Container ${container} not found in ${kind}/${name}. Containers: ${names}" >&2
    exit 1
  fi

  # Ensure volumeMounts array exists (some manifests omit it)
  if ! kubectl -n "$NAMESPACE" get "$kind/$name" \
        -o jsonpath="{.spec.template.spec.containers[$idx].volumeMounts}" 2>/dev/null | grep -q .; then
    kubectl -n "$NAMESPACE" patch "$kind/$name" --type='json' \
      -p "$(printf '[{"op":"add","path":"/spec/template/spec/containers/%s/volumeMounts","value":[]}]' "$idx")"
  fi

  # Add volumeMount if missing
  if ! kubectl -n "$NAMESPACE" get "$kind/$name" \
        -o jsonpath="{.spec.template.spec.containers[$idx].volumeMounts[*].mountPath}" \
        2>/dev/null | tr ' ' '\n' | grep -qx "$mnt"; then
    local mnt_patch
    mnt_patch=$(printf '[{"op":"add","path":"/spec/template/spec/containers/%s/volumeMounts/-","value":{"name":"%s","mountPath":"%s"}}]' \
      "$idx" "$vol_name" "$mnt")
    kubectl -n "$NAMESPACE" patch "$kind/$name" --type='json' -p "$mnt_patch"
  fi
}


ensure_mount statefulset slurm-controller slurm-controller shared-storage slurm-shared-rwx /shared
ensure_mount statefulset slurm-worker slurm-worker shared-storage slurm-shared-rwx /shared

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT"

echo "Phase 3 storage deployment completed."
echo "NFS_SERVER=${NFS_SERVER}, NFS_PATH=${NFS_PATH}"
