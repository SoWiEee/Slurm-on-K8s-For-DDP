#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
NAMESPACE=${NAMESPACE:-slurm}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
REGENERATE_SECRETS=${REGENERATE_SECRETS:-false}

validate_rendered_manifest() {
  if ! grep -q '^\s*command:$' phase1/manifests/slurm-static.yaml; then
    echo "phase1 rendered slurm-static.yaml does not contain explicit command blocks" >&2
    exit 1
  fi
}

validate_live_command() {
  local res="$1"
  local live
  live=$(kubectl -n "$NAMESPACE" get "$res" -o jsonpath='{.spec.template.spec.containers[0].command[0]}' 2>/dev/null || true)
  if [[ "$live" != "/bin/bash" ]]; then
    echo "phase1 live $res command[0] is '$live', expected /bin/bash" >&2
    kubectl -n "$NAMESPACE" get "$res" -o yaml >&2 || true
    exit 1
  fi
}

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

if [[ -f phase1/scripts/render-slurm-static.py ]]; then
  if py -3 phase1/scripts/render-slurm-static.py 2>/dev/null; then
    true
  else
    python3 phase1/scripts/render-slurm-static.py
  fi
  echo "[phase1 bootstrap] phase1 manifests rendered."
fi
validate_rendered_manifest

REGENERATE_SECRETS="$REGENERATE_SECRETS" phase1/scripts/create-secrets.sh "$NAMESPACE"
echo "[phase1 bootstrap] applying phase1 manifests..."
kubectl apply -f phase1/manifests/slurm-ddp-runtime.yaml
kubectl apply -f phase1/manifests/slurm-static.yaml
if [[ -f phase1/manifests/slurm-login.yaml ]]; then
  kubectl apply -f phase1/manifests/slurm-login.yaml
fi
# Deploy accounting stack (MySQL + slurmdbd) if manifest exists.
if [[ -f phase1/manifests/slurm-accounting.yaml ]]; then
  echo "[phase1 bootstrap] deploying accounting stack (MySQL + slurmdbd)..."
  kubectl apply -f phase1/manifests/slurm-accounting.yaml
fi
validate_live_command statefulset/slurm-controller
validate_live_command statefulset/slurm-worker-cpu

for r in statefulset/slurm-controller statefulset/slurm-worker-cpu deployment/slurm-login; do
  kubectl -n "$NAMESPACE" get "$r" >/dev/null 2>&1 && kubectl -n "$NAMESPACE" rollout restart "$r" >/dev/null 2>&1 || true
done

kubectl -n "$NAMESPACE" delete pod -l app=slurm-controller --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-cpu --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-login --ignore-not-found=true >/dev/null 2>&1 || true

kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker-cpu --timeout="$ROLLOUT_TIMEOUT"

controller_pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-controller -o jsonpath='{.items[0].metadata.name}')
for _ in $(seq 1 60); do
  if kubectl -n "$NAMESPACE" exec "pod/${controller_pod}" -- bash -lc 'scontrol ping >/dev/null 2>&1'; then
    break
  fi
  sleep 2
done

# Wait for accounting stack if it was deployed.
if [[ -f phase1/manifests/slurm-accounting.yaml ]]; then
  echo "[phase1 bootstrap] waiting for MySQL readiness..."
  kubectl -n "$NAMESPACE" rollout status statefulset/mysql --timeout="$ROLLOUT_TIMEOUT" || true
  echo "[phase1 bootstrap] waiting for slurmdbd readiness..."
  kubectl -n "$NAMESPACE" rollout status deployment/slurmdbd --timeout="$ROLLOUT_TIMEOUT" || true
fi

echo "Phase 1 bootstrap completed."
