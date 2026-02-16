#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KIND_CONFIG=${KIND_CONFIG:-}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
DOCKER_BUILD_NO_CACHE=${DOCKER_BUILD_NO_CACHE:-false}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
FORCE_RECREATE=${FORCE_RECREATE:-false}

echo "[dev bootstrap] validating tools..."
if ! command -v kind >/dev/null 2>&1; then
  echo "kind is required" >&2
  exit 1
fi
if ! command -v kubectl >/dev/null 2>&1; then
  echo "kubectl is required" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required" >&2
  exit 1
fi

echo "[dev bootstrap] ensuring kind cluster '${CLUSTER_NAME}' exists..."
if ! kind get clusters | grep -q "^${CLUSTER_NAME}$"; then
  if [[ -n "$KIND_CONFIG" ]]; then
    kind create cluster --name "$CLUSTER_NAME" --config "$KIND_CONFIG"
  else
    kind create cluster --name "$CLUSTER_NAME"
  fi
fi

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found. available contexts:" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

build_flags=()
if [[ "$DOCKER_BUILD_NO_CACHE" == "true" ]]; then
  build_flags+=(--no-cache)
fi

echo "[dev bootstrap] building phase1 images..."
docker build "${build_flags[@]}" -t slurm-controller:phase1 phase1/docker/controller
docker build "${build_flags[@]}" -t slurm-worker:phase1 phase1/docker/worker

echo "[dev bootstrap] loading phase1 images to kind..."
kind load docker-image slurm-controller:phase1 --name "$CLUSTER_NAME"
kind load docker-image slurm-worker:phase1 --name "$CLUSTER_NAME"

echo "[dev bootstrap] applying phase1 secrets and manifests..."
phase1/scripts/create-secrets.sh "$NAMESPACE"

if [[ "$FORCE_RECREATE" == "true" ]]; then
  kubectl -n "$NAMESPACE" delete statefulset slurm-controller slurm-worker --ignore-not-found=true
  kubectl -n "$NAMESPACE" delete pod -l app=slurm-controller --ignore-not-found=true
  kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker --ignore-not-found=true
fi

kubectl apply -f phase1/manifests/slurm-static.yaml
kubectl -n "$NAMESPACE" rollout restart statefulset/slurm-controller statefulset/slurm-worker || true

if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  echo "namespace '$NAMESPACE' not found after apply; check current context: $(kubectl config current-context)" >&2
  exit 1
fi

if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller slurm-worker >/dev/null 2>&1; then
  echo "required phase1 statefulsets not found in namespace '$NAMESPACE'" >&2
  kubectl -n "$NAMESPACE" get all || true
  exit 1
fi

set +e
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-controller --timeout="$ROLLOUT_TIMEOUT"
rc1=$?
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"
rc2=$?
set -e

if [[ $rc1 -ne 0 || $rc2 -ne 0 ]]; then
  echo "[dev bootstrap] phase1 rollout failed, collecting diagnostics..." >&2
  kubectl -n "$NAMESPACE" get all -o wide || true
  kubectl -n "$NAMESPACE" describe statefulset slurm-controller slurm-worker || true
  kubectl -n "$NAMESPACE" describe pods || true
  for p in $(kubectl -n "$NAMESPACE" get pods -o name 2>/dev/null); do
    kubectl -n "$NAMESPACE" logs "$p" --all-containers=true --tail=200 || true
    kubectl -n "$NAMESPACE" logs "$p" --all-containers=true --previous --tail=200 || true
    kubectl -n "$NAMESPACE" exec "$p" -- sh -c 'ls -lah /slurm-secrets 2>/dev/null || true' || true
  done
  exit 1
fi

echo "[dev bootstrap] building phase2 operator image..."
docker build "${build_flags[@]}" -t slurm-elastic-operator:phase2 -f phase2/docker/operator/Dockerfile .

echo "[dev bootstrap] loading phase2 image to kind..."
kind load docker-image slurm-elastic-operator:phase2 --name "$CLUSTER_NAME"

echo "[dev bootstrap] applying phase2 operator manifest..."
kubectl apply -f phase2/manifests/slurm-phase2-operator.yaml

# Keep a single worker at start so operator scale-up path is observable.
kubectl -n "$NAMESPACE" scale statefulset/slurm-worker --replicas=1

kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$ROLLOUT_TIMEOUT"
kubectl -n "$NAMESPACE" rollout status statefulset/slurm-worker --timeout="$ROLLOUT_TIMEOUT"

echo "[dev bootstrap] done. phase1 + phase2 deployed on context: ${KUBE_CONTEXT}"
