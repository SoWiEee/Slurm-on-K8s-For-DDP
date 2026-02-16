#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
NFS_SERVER=${NFS_SERVER:-}
NFS_PATH=${NFS_PATH:-/srv/nfs/k8s}
PROVISIONER_NAMESPACE=${PROVISIONER_NAMESPACE:-nfs-provisioner}

step="init"

print_access_denied_fix() {
  local node_ips
  node_ips=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.addresses[?(@.type=="InternalIP")].address}{" "}{end}' 2>/dev/null || true)

  cat >&2 <<FIX
[phase3 bootstrap][detected] kubelet 回報 "mount.nfs: access denied by server"
這通常是 NFS server 的 /etc/exports 未允許 Kind 節點來源位址。

請在 NFS Server (WSL/VM) 檢查：
1) export 路徑是否存在：
   sudo ls -ld ${NFS_PATH}
2) exports 是否包含 Kind node 網段或 node IP：
   # 參考 node internal IP: ${node_ips:-unknown}
   ${NFS_PATH} 172.16.0.0/12(rw,sync,no_subtree_check,no_root_squash,insecure)
3) 重新套用 export：
   sudo exportfs -ra
   sudo exportfs -v
4) 確保 NFS 服務可用：
   sudo systemctl status nfs-server || sudo systemctl status nfs-kernel-server
FIX
}

analyze_mount_failures() {
  local event_text
  event_text=$(kubectl -n "$PROVISIONER_NAMESPACE" get events --sort-by=.metadata.creationTimestamp 2>/dev/null || true)
  if echo "$event_text" | grep -q 'access denied by server while mounting'; then
    print_access_denied_fix
  fi

  if echo "$event_text" | grep -q 'No route to host\|Connection timed out\|Connection refused'; then
    cat >&2 <<NET
[phase3 bootstrap][detected] 可能是 NFS 網路連線問題（路由/防火牆）
請先確認 WSL/VM 防火牆與路由允許 2049/tcp。
NET
  fi
}

print_hint() {
  cat >&2 <<'HINT'
[phase3 bootstrap][possible-causes]
1) Kind node 無法連到 NFS_SERVER:2049（IP/路由/Windows Firewall）。
2) NFS export 未放行 Kind Docker 網段（/etc/exports CIDR 不含 kind node IP）。
3) NFS_PATH 不存在或未 export。
4) nfs-subdir-external-provisioner Pod 啟動後 mount NFS 失敗（可看 describe/logs）。
5) context/namespace 誤用，實際套用到非預期叢集。
HINT
}

dump_debug_info() {
  echo "[phase3 bootstrap][debug] failed_step=${step}" >&2
  echo "[phase3 bootstrap][debug] context=$(kubectl config current-context 2>/dev/null || echo unknown)" >&2
  echo "[phase3 bootstrap][debug] nfs_server=${NFS_SERVER} nfs_path=${NFS_PATH}" >&2

  echo "[phase3 bootstrap][debug] kind nodes ip (if available)" >&2
  kubectl get nodes -o wide >&2 || true

  echo "[phase3 bootstrap][debug] provisioner deployment" >&2
  kubectl -n "$PROVISIONER_NAMESPACE" get deploy,pods -o wide >&2 || true

  echo "[phase3 bootstrap][debug] describe provisioner deployment" >&2
  kubectl -n "$PROVISIONER_NAMESPACE" describe deployment nfs-subdir-external-provisioner >&2 || true

  echo "[phase3 bootstrap][debug] provisioner pod describe/logs" >&2
  local pod
  pod=$(kubectl -n "$PROVISIONER_NAMESPACE" get pod -l app=nfs-subdir-external-provisioner -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -n "$pod" ]]; then
    kubectl -n "$PROVISIONER_NAMESPACE" describe pod "$pod" >&2 || true
    kubectl -n "$PROVISIONER_NAMESPACE" logs "$pod" --tail=200 >&2 || true
  fi

  echo "[phase3 bootstrap][debug] latest warning events (nfs-provisioner/slurm)" >&2
  kubectl -n "$PROVISIONER_NAMESPACE" get events --sort-by=.metadata.creationTimestamp >&2 || true
  kubectl -n "$NAMESPACE" get events --sort-by=.metadata.creationTimestamp >&2 || true

  echo "[phase3 bootstrap][debug] pvc/pv status" >&2
  kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx -o wide >&2 || true
  kubectl get pv >&2 || true

  analyze_mount_failures
  print_hint
}

on_error() {
  dump_debug_info
}

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

trap on_error ERR

rendered_manifest=$(mktemp)
trap 'rm -f "$rendered_manifest"' EXIT

step="render_manifest"
sed -e "s|__NFS_SERVER__|${NFS_SERVER}|g" \
    -e "s|__NFS_PATH__|${NFS_PATH}|g" \
    phase3/manifests/nfs-subdir-provisioner.tmpl.yaml > "$rendered_manifest"

echo "[phase3 bootstrap] context=${KUBE_CONTEXT} namespace=${NAMESPACE}"
echo "[phase3 bootstrap] nfs_server=${NFS_SERVER} nfs_path=${NFS_PATH}"

echo "[phase3 bootstrap] applying nfs provisioner manifests"
step="apply_provisioner"
kubectl apply -f "$rendered_manifest"

echo "[phase3 bootstrap] waiting provisioner rollout timeout=${ROLLOUT_TIMEOUT}"
step="wait_provisioner_rollout"
kubectl -n "$PROVISIONER_NAMESPACE" rollout status deployment/nfs-subdir-external-provisioner --timeout="$ROLLOUT_TIMEOUT"

echo "[phase3 bootstrap] applying shared storage resources"
step="apply_shared_storage"
kubectl apply -f phase3/manifests/shared-storage.yaml

echo "[phase3 bootstrap] waiting pvc bound timeout=${ROLLOUT_TIMEOUT}"
step="wait_pvc_bound"
kubectl -n "$NAMESPACE" wait --for=condition=Bound pvc/slurm-shared-rwx --timeout="$ROLLOUT_TIMEOUT"

step="patch_controller_mount"
patch='{"spec":{"template":{"spec":{"volumes":[{"name":"shared-storage","persistentVolumeClaim":{"claimName":"slurm-shared-rwx"}}],"containers":[{"name":"slurm-controller","volumeMounts":[{"name":"shared-storage","mountPath":"/shared"}]}]}}}}'
kubectl -n "$NAMESPACE" patch statefulset slurm-controller --type strategic -p "$patch"

step="patch_worker_mount"
patch='{"spec":{"template":{"spec":{"volumes":[{"name":"shared-storage","persistentVolumeClaim":{"claimName":"slurm-shared-rwx"}}],"containers":[{"name":"slurm-worker","volumeMounts":[{"name":"shared-storage","mountPath":"/shared"}]}]}}}}'
kubectl -n "$NAMESPACE" patch statefulset slurm-worker --type strategic -p "$patch"

step="wait_slurm_rollout"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT"

echo "Phase 3 storage deployment completed."
echo "NFS_SERVER=${NFS_SERVER}, NFS_PATH=${NFS_PATH}"
