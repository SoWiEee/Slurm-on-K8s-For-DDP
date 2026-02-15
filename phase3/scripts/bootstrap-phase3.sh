#!/usr/bin/env bash
set -Eeuo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-180s}
PVC_BOUND_TIMEOUT=${PVC_BOUND_TIMEOUT:-180s}
POST_CHECK_RETRIES=${POST_CHECK_RETRIES:-6}
POST_CHECK_INTERVAL_SECONDS=${POST_CHECK_INTERVAL_SECONDS:-5}

on_error() {
  local exit_code=$?
  echo "[phase3/bootstrap] failed (exit=${exit_code}), dumping diagnostics..." >&2
  {
    echo "--- context ---"
    kubectl config current-context
    echo
    echo "--- pvc ---"
    kubectl -n "$NAMESPACE" get pvc slurm-shared-pvc -o wide || true
    kubectl -n "$NAMESPACE" describe pvc slurm-shared-pvc || true
    echo
    echo "--- pv ---"
    kubectl get pv || true
    echo
    echo "--- storageclass ---"
    kubectl get storageclass || true
    echo
    echo "--- statefulsets ---"
    kubectl -n "$NAMESPACE" get sts slurm-controller slurm-worker -o wide || true
    echo
    echo "--- pods ---"
    kubectl -n "$NAMESPACE" get pods -o wide || true
    echo
    echo "--- controller /shared ---"
    kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sh -c 'ls -ld /shared; mount | grep /shared || true' || true
    echo
    echo "--- worker /shared ---"
    kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- sh -c 'ls -ld /shared; mount | grep /shared || true' || true
    echo
    echo "--- recent events ---"
    kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -n 80 || true
  } >&2
  exit "$exit_code"
}
trap on_error ERR

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

kubectl -n "$NAMESPACE" get statefulset slurm-controller >/dev/null
kubectl -n "$NAMESPACE" get statefulset slurm-worker >/dev/null

kubectl apply -f phase3/manifests/shared-storage.yaml

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

# NOTE:
# Some StorageClasses (e.g. local-path with WaitForFirstConsumer) only bind PVC
# after a Pod uses it. Therefore PVC Bound check is intentionally after rollout.
kubectl -n "$NAMESPACE" wait --for=jsonpath='{.status.phase}'=Bound pvc/slurm-shared-pvc --timeout="$PVC_BOUND_TIMEOUT"

kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-controller-0 --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" wait --for=condition=Ready pod/slurm-worker-0 --timeout="$ROLLOUT_TIMEOUT"

check_shared_ready() {
  kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sh -c 'test -d /shared && touch /shared/.phase3-controller && rm -f /shared/.phase3-controller'
  kubectl -n "$NAMESPACE" exec pod/slurm-worker-0 -- sh -c 'test -d /shared && touch /shared/.phase3-worker && rm -f /shared/.phase3-worker'
}

attempt=1
until check_shared_ready; do
  if (( attempt >= POST_CHECK_RETRIES )); then
    echo "[phase3/bootstrap] /shared post-check failed after ${POST_CHECK_RETRIES} attempts" >&2
    exit 1
  fi
  echo "[phase3/bootstrap] /shared post-check failed, retry ${attempt}/${POST_CHECK_RETRIES} ..." >&2
  sleep "$POST_CHECK_INTERVAL_SECONDS"
  attempt=$((attempt + 1))
done

echo "Phase 3 shared storage bootstrap complete."
