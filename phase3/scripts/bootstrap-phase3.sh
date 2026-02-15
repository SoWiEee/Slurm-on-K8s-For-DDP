#!/usr/bin/env bash
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-slurm-lab}"
NAMESPACE="${NAMESPACE:-slurm}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
PHASE3_STORAGE_CLASS="${PHASE3_STORAGE_CLASS:-}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="${ROOT_DIR}/phase3/manifests/slurm-phase3-shared.yaml"

log() {
  printf '[phase3 bootstrap] %s\n' "$*"
}

contains_line() {
  local needle="$1"
  grep -Fxq "${needle}"
}

ensure_context() {
  kubectl config use-context "${KUBE_CONTEXT}" >/dev/null
  log "using context: ${KUBE_CONTEXT}"
}

ensure_phase1_ready() {
  log "checking phase1 components"
  kubectl -n "${NAMESPACE}" rollout status statefulset/slurm-controller --timeout="${ROLLOUT_TIMEOUT}"
  kubectl -n "${NAMESPACE}" rollout status statefulset/slurm-worker --timeout="${ROLLOUT_TIMEOUT}"
}

resolve_storage_class() {
  if [[ -n "${PHASE3_STORAGE_CLASS}" ]]; then
    if ! kubectl get storageclass "${PHASE3_STORAGE_CLASS}" >/dev/null 2>&1; then
      log "指定的 PHASE3_STORAGE_CLASS=${PHASE3_STORAGE_CLASS} 不存在"
      kubectl get storageclass || true
      exit 1
    fi
    echo "${PHASE3_STORAGE_CLASS}"
    return
  fi

  if kubectl get storageclass local-path >/dev/null 2>&1; then
    echo "local-path"
    return
  fi

  local default_sc
  default_sc="$(kubectl get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\t"}{.metadata.annotations.storageclass\.beta\.kubernetes\.io/is-default-class}{"\n"}{end}' | awk '$2=="true" || $3=="true" {print $1; exit}')"
  if [[ -n "${default_sc}" ]]; then
    echo "${default_sc}"
    return
  fi

  local first_sc
  first_sc="$(kubectl get storageclass -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [[ -n "${first_sc}" ]]; then
    echo "${first_sc}"
    return
  fi

  log "找不到可用 StorageClass，請先建立 StorageClass，或指定 PHASE3_STORAGE_CLASS"
  exit 1
}

ensure_shared_pvc() {
  local sc="$1"
  local pvc="slurm-shared"

  if kubectl -n "${NAMESPACE}" get pvc "${pvc}" >/dev/null 2>&1; then
    local current_sc status
    current_sc="$(kubectl -n "${NAMESPACE}" get pvc "${pvc}" -o jsonpath='{.spec.storageClassName}')"
    status="$(kubectl -n "${NAMESPACE}" get pvc "${pvc}" -o jsonpath='{.status.phase}')"

    if [[ "${current_sc}" != "${sc}" ]]; then
      if [[ "${status}" == "Bound" ]]; then
        log "既有 PVC ${pvc} 已 Bound 但 StorageClass=${current_sc}，與目標 ${sc} 不同，為避免資料風險請手動處理"
        exit 1
      fi
      log "重建 Pending PVC ${pvc}（${current_sc} -> ${sc}）"
      kubectl -n "${NAMESPACE}" delete pvc "${pvc}" --wait=true
    fi
  fi

  cat <<YAML | kubectl apply -f -
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${pvc}
  namespace: ${NAMESPACE}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 5Gi
  storageClassName: ${sc}
YAML

  if ! kubectl -n "${NAMESPACE}" wait pvc/${pvc} --for=jsonpath='{.status.phase}'=Bound --timeout="${ROLLOUT_TIMEOUT}"; then
    log "PVC ${pvc} 在 ${ROLLOUT_TIMEOUT} 內未 Bound（storageClass=${sc}）"
    kubectl -n "${NAMESPACE}" get pvc "${pvc}" -o wide || true
    kubectl -n "${NAMESPACE}" describe pvc "${pvc}" || true
    kubectl get storageclass || true
    exit 1
  fi
}

apply_phase3_basics() {
  local sc="$1"
  log "applying phase3 shared storage + job templates"
  log "selected storageClass: ${sc}"
  ensure_shared_pvc "${sc}"
  kubectl apply -f "${MANIFEST}"
}

patch_shared_mount() {
  local target="$1"
  local volume_names
  local mount_names

  log "ensuring /shared mount on ${target}"

  volume_names="$(kubectl -n "${NAMESPACE}" get statefulset "${target}" -o jsonpath='{.spec.template.spec.volumes[*].name}' | tr ' ' '\n')"
  if printf '%s\n' "${volume_names}" | contains_line 'shared-storage'; then
    log "${target}: shared-storage volume already exists"
  else
    kubectl -n "${NAMESPACE}" patch statefulset "${target}" --type='json' \
      -p='[{"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"shared-storage","persistentVolumeClaim":{"claimName":"slurm-shared"}}}]'
  fi

  mount_names="$(kubectl -n "${NAMESPACE}" get statefulset "${target}" -o jsonpath='{.spec.template.spec.containers[0].volumeMounts[*].name}' | tr ' ' '\n')"
  if printf '%s\n' "${mount_names}" | contains_line 'shared-storage'; then
    log "${target}: /shared mount already exists"
  else
    kubectl -n "${NAMESPACE}" patch statefulset "${target}" --type='json' \
      -p='[{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"shared-storage","mountPath":"/shared"}}]'
  fi

  kubectl -n "${NAMESPACE}" rollout status statefulset/"${target}" --timeout="${ROLLOUT_TIMEOUT}"
}

main() {
  local sc
  ensure_context
  ensure_phase1_ready
  sc="$(resolve_storage_class)"
  apply_phase3_basics "${sc}"
  patch_shared_mount slurm-controller
  patch_shared_mount slurm-worker
  log "phase3 bootstrap done"
}

main "$@"
