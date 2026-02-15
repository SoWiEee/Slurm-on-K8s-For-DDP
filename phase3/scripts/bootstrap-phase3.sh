#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-180s}

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

kubectl -n "$NAMESPACE" get statefulset slurm-controller >/dev/null
kubectl -n "$NAMESPACE" get statefulset slurm-worker >/dev/null

kubectl apply -f phase3/manifests/shared-storage.yaml
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Bound pvc/slurm-shared-pvc --timeout=120s

patch_statefulset() {
  local sts_name=$1
  local container_name=$2

  kubectl -n "$NAMESPACE" patch statefulset "$sts_name" --type='strategic' --patch "$(cat <<PATCH
spec:
  template:
    spec:
      containers:
        - name: ${container_name}
          volumeMounts:
            - name: shared-storage
              mountPath: /shared
      volumes:
        - name: shared-storage
          persistentVolumeClaim:
            claimName: slurm-shared-pvc
PATCH
)"
}

patch_statefulset "slurm-controller" "slurm-controller"
patch_statefulset "slurm-worker" "slurm-worker"

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"

kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- test -d /shared
kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- test -d /shared

echo "Phase 3 shared storage bootstrap complete."
