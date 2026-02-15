#!/usr/bin/env bash
set -euo pipefail

KUBE_CONTEXT="${KUBE_CONTEXT:-kind-slurm-lab}"
NAMESPACE="${NAMESPACE:-slurm}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MANIFEST="${ROOT_DIR}/phase3/manifests/slurm-phase3-shared.yaml"

log() {
  printf '[phase3 bootstrap] %s\n' "$*"
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

apply_phase3_basics() {
  log "applying phase3 shared storage + job templates"
  kubectl apply -f "${MANIFEST}"
  kubectl -n "${NAMESPACE}" wait pvc/slurm-shared --for=jsonpath='{.status.phase}'=Bound --timeout="${ROLLOUT_TIMEOUT}" || true
}

patch_shared_mount() {
  local target="$1"
  log "ensuring /shared mount on ${target}"

  if kubectl -n "${NAMESPACE}" get statefulset "${target}" -o jsonpath='{.spec.template.spec.volumes[*].name}' | tr ' ' '\n' | rg -qx 'shared-storage'; then
    log "${target}: shared-storage volume already exists"
  else
    kubectl -n "${NAMESPACE}" patch statefulset "${target}" --type='json' \
      -p='[{"op":"add","path":"/spec/template/spec/volumes/-","value":{"name":"shared-storage","persistentVolumeClaim":{"claimName":"slurm-shared"}}}]'
  fi

  if kubectl -n "${NAMESPACE}" get statefulset "${target}" -o jsonpath='{.spec.template.spec.containers[0].volumeMounts[*].name}' | tr ' ' '\n' | rg -qx 'shared-storage'; then
    log "${target}: /shared mount already exists"
  else
    kubectl -n "${NAMESPACE}" patch statefulset "${target}" --type='json' \
      -p='[{"op":"add","path":"/spec/template/spec/containers/0/volumeMounts/-","value":{"name":"shared-storage","mountPath":"/shared"}}]'
  fi

  kubectl -n "${NAMESPACE}" rollout status statefulset/"${target}" --timeout="${ROLLOUT_TIMEOUT}"
}

main() {
  ensure_context
  ensure_phase1_ready
  apply_phase3_basics
  patch_shared_mount slurm-controller
  patch_shared_mount slurm-worker
  log "phase3 bootstrap done"
}

main "$@"
