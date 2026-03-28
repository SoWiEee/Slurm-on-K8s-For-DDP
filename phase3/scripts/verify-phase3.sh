#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

kubectl get storageclass slurm-shared-nfs >/dev/null
kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx >/dev/null

# NOTE: PVC "Bound" is a phase (.status.phase), not a Condition. Use polling on phase instead of `kubectl wait --for=condition=Bound`.
echo "[verify] Waiting for PVC slurm-shared-rwx to become Bound..."
wait_seconds() {
  local t="$1"
  if [[ "$t" =~ ^[0-9]+s$ ]]; then echo "${t%s}"; return; fi
  if [[ "$t" =~ ^[0-9]+m$ ]]; then echo "$(( ${t%m} * 60 ))"; return; fi
  if [[ "$t" =~ ^([0-9]+)m([0-9]+)s$ ]]; then echo "$(( ${BASH_REMATCH[1]} * 60 + ${BASH_REMATCH[2]} ))"; return; fi
  echo 300
}
deadline=$(( $(date +%s) + $(wait_seconds "$ROLLOUT_TIMEOUT") ))
while true; do
  phase="$(kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx -o jsonpath='{.status.phase}' 2>/dev/null || true)"
  if [[ "$phase" == "Bound" ]]; then
    echo "[verify] PVC slurm-shared-rwx is Bound."
    break
  fi
  if (( $(date +%s) >= deadline )); then
    echo "[verify][ERROR] Timed out waiting for PVC slurm-shared-rwx to become Bound." >&2
    kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx -o yaml >&2 || true
    kubectl -n "$NAMESPACE" describe pvc slurm-shared-rwx >&2 || true
    exit 1
  fi
  sleep 2
done

echo "[verify] Checking /shared mount + write test in slurm-login..."
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT" >/dev/null
kubectl -n "$NAMESPACE" exec deploy/slurm-login -- sh -lc \
  'set -eu; mount | grep -E " on /shared (type|)" >/dev/null; echo verify-$(date +%s) > /shared/.verify-test; tail -n 1 /shared/.verify-test'

echo "[verify] Checking /shared mount in slurm-controller..."
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT" >/dev/null
kubectl -n "$NAMESPACE" exec statefulset/slurm-controller -- sh -lc \
  'set -eu; mount | grep -E " on /shared (type|)" >/dev/null; ls -la /shared | head'

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker-cpu --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status deployment/slurm-login --timeout="$ROLLOUT_TIMEOUT"

for pod in slurm-controller-0 slurm-worker-cpu-0; do
  echo "[verify] Checking /shared in $pod ..."
  kubectl -n "$NAMESPACE" exec "pod/$pod" -- sh -lc 'test -d /shared && echo OK: /shared exists'
done

login_pod="$(kubectl -n "$NAMESPACE" get pod -l app=slurm-login -o jsonpath='{.items[0].metadata.name}')"
echo "[verify] Checking /shared in login pod ${login_pod} ..."
kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- sh -lc 'test -d /shared && echo OK: /shared exists'
kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- sh -lc 'mount | grep -E " on /shared (type|)" >/dev/null'

marker="phase3-$(date +%s)"
kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sh -lc "printf '%s\n' '${marker}' > /shared/.phase3-marker"
kubectl -n "$NAMESPACE" exec pod/slurm-worker-cpu-0 -- sh -lc "grep -Fqx '${marker}' /shared/.phase3-marker"
kubectl -n "$NAMESPACE" exec "pod/${login_pod}" -- sh -lc "grep -Fqx '${marker}' /shared/.phase3-marker"

echo "Phase 3 verification passed: shared RWX volume is mounted across controller/worker/login."
