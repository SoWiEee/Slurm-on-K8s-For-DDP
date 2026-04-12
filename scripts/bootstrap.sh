#!/usr/bin/env bash
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
KIND_CONFIG=${KIND_CONFIG:-}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
DOCKER_BUILD_NO_CACHE=${DOCKER_BUILD_NO_CACHE:-false}
NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
FORCE_RECREATE=${FORCE_RECREATE:-false}
REGENERATE_SECRETS=${REGENERATE_SECRETS:-false}

log() {
  echo "[bootstrap] $*"
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || { echo "$1 is required" >&2; exit 1; }
}

rollout_or_dump() {
  local kindname="$1"
  if ! kubectl -n "$NAMESPACE" rollout status "$kindname" --timeout="$ROLLOUT_TIMEOUT"; then
    echo "[bootstrap] rollout failed for $kindname, collecting diagnostics..." >&2
    kubectl -n "$NAMESPACE" get all -o wide >&2 || true
    kubectl -n "$NAMESPACE" describe "$kindname" >&2 || true
    kubectl -n "$NAMESPACE" describe pods >&2 || true
    for p in $(kubectl -n "$NAMESPACE" get pods -o name 2>/dev/null); do
      kubectl -n "$NAMESPACE" logs "$p" --all-containers=true --tail=200 >&2 || true
      kubectl -n "$NAMESPACE" logs "$p" --all-containers=true --previous --tail=200 >&2 || true
    done
    exit 1
  fi
}

maybe_rollout_restart() {
  local resource="$1"
  kubectl -n "$NAMESPACE" get "$resource" >/dev/null 2>&1 || return 0
  kubectl -n "$NAMESPACE" rollout restart "$resource" >/dev/null 2>&1 || true
}

wait_slurm_ready() {
  local controller_pod="$1"
  log "waiting for slurmctld to answer scontrol ping from controller pod..."
  local deadline=$(( $(date +%s) + 180 ))
  while true; do
    if kubectl -n "$NAMESPACE" exec "pod/${controller_pod}" -- bash -lc 'scontrol ping >/dev/null 2>&1'; then
      break
    fi
    if (( $(date +%s) >= deadline )); then
      echo "[bootstrap] slurmctld did not become responsive in time" >&2
      kubectl -n "$NAMESPACE" logs "pod/${controller_pod}" --tail=200 >&2 || true
      exit 1
    fi
    sleep 3
  done
}

log "validating tools..."
require_tool kind
require_tool kubectl
require_tool docker

# Resolve a working Python 3 interpreter.
# Override with PYTHON=/path/to/python3 if auto-detection fails.
if [[ -z "${PYTHON:-}" ]]; then
  for _py in python3 python py; do
    if command -v "$_py" >/dev/null 2>&1 && "$_py" -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" 2>/dev/null; then
      PYTHON="$_py"; break
    fi
  done
  : "${PYTHON:?Cannot find a working Python 3.8+ interpreter. Set PYTHON=/path/to/python3}"
fi

validate_live_commands() {
  local res="$1"
  local expected="$2"
  kubectl -n "$NAMESPACE" get "$res" >/dev/null 2>&1 || return 0
  local live
  live=$(kubectl -n "$NAMESPACE" get "$res" -o jsonpath='{.spec.template.spec.containers[0].command[0]}' 2>/dev/null || true)
  if [[ "$live" != "$expected" ]]; then
    echo "[bootstrap] ERROR: live $res container command[0]='$live' expected '$expected'" >&2
    kubectl -n "$NAMESPACE" get "$res" -o yaml >&2 || true
    exit 1
  fi
}

validate_rendered_manifest() {
  if ! grep -q '^\s*command:$' manifests/core/slurm-static.yaml; then
    echo "[bootstrap] ERROR: rendered slurm-static.yaml does not contain explicit command blocks" >&2
    exit 1
  fi
}


operator_force_env() {
  local partitions_json='[
    {"partition":"debug","worker_statefulset":"slurm-worker-cpu","min_replicas":1,"max_replicas":4,"scale_up_step":1,"scale_down_step":1,"scale_down_cooldown":60,"match_features":["cpu"],"fallback":true},
    {"partition":"debug","worker_statefulset":"slurm-worker-gpu-a10","min_replicas":0,"max_replicas":4,"scale_up_step":1,"scale_down_step":1,"scale_down_cooldown":60,"match_features":["gpu-a10"],"match_gres":["gpu:a10"]},
    {"partition":"debug","worker_statefulset":"slurm-worker-gpu-h100","min_replicas":0,"max_replicas":4,"scale_up_step":1,"scale_down_step":1,"scale_down_cooldown":60,"match_features":["gpu-h100"],"match_gres":["gpu:h100"]}
  ]'

  kubectl -n "$NAMESPACE" set env deployment/slurm-elastic-operator \
    NAMESPACE="$NAMESPACE" \
    CONTROLLER_POD="slurm-controller-0" \
    SLURM_PARTITION="debug" \
    WORKER_STATEFULSET="slurm-worker-cpu" \
    PARTITIONS_JSON="$partitions_json" \
    MIN_REPLICAS="1" \
    MAX_REPLICAS="4" \
    SCALE_UP_STEP="1" \
    SCALE_DOWN_STEP="1" \
    POLL_INTERVAL_SECONDS="15" \
    SCALE_DOWN_COOLDOWN_SECONDS="60" \
    CHECKPOINT_GUARD_ENABLED="true" \
    CHECKPOINT_PATH="" \
    MAX_CHECKPOINT_AGE_SECONDS="600" \
    SLURM_REST_URL="http://slurm-restapi.${NAMESPACE}.svc.cluster.local:6820" \
    SLURM_REST_API_VERSION="v0.0.37" >/dev/null
    # SLURM_JWT_KEY_PATH is set in the manifest; omit here to avoid Git Bash
    # POSIX path conversion (/ → C:/Program Files/Git/) on Windows.
}

validate_live_operator_config() {
  kubectl -n "$NAMESPACE" get deployment/slurm-elastic-operator >/dev/null 2>&1 || return 0
  local part_json
  part_json=$(kubectl -n "$NAMESPACE" get deployment slurm-elastic-operator -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="PARTITIONS_JSON")].value}' 2>/dev/null || true)
  if [[ -z "$part_json" ]]; then
    echo "[bootstrap] ERROR: live slurm-elastic-operator deployment does not contain PARTITIONS_JSON" >&2
    kubectl -n "$NAMESPACE" get deployment slurm-elastic-operator -o yaml >&2 || true
    exit 1
  fi
}

log "ensuring kind cluster '${CLUSTER_NAME}' exists..."
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

log "building core images..."
docker build "${build_flags[@]}" -t slurm-controller:latest docker/controller
docker build "${build_flags[@]}" -t slurm-worker:latest docker/worker

log "loading core images to kind..."
kind load docker-image slurm-controller:latest --name "$CLUSTER_NAME"
kind load docker-image slurm-worker:latest --name "$CLUSTER_NAME"

log "rendering core manifests (if generator exists)..."
if [[ -f scripts/render-core.py ]]; then
  render_flags=(--with-lmod)
  if kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx >/dev/null 2>&1; then
    render_flags+=(--with-shared-storage)
    log "NFS PVC detected — rendering with shared storage"
  fi
  render_rc=0
  "$PYTHON" scripts/render-core.py "${render_flags[@]}" || render_rc=$?
  if [[ ! -s manifests/core/slurm-static.yaml ]]; then
    echo "[bootstrap] ERROR: render script failed and manifests/core/slurm-static.yaml is missing or empty" >&2
    exit "${render_rc:-1}"
  fi
  if ! grep -q '^kind: StatefulSet$' manifests/core/slurm-static.yaml; then
    echo "[bootstrap] ERROR: rendered slurm-static.yaml looks incomplete" >&2
    exit 1
  fi
  if [[ ${render_rc:-0} -ne 0 ]]; then
    echo "[bootstrap] warning: render script exited with code ${render_rc}, but output file exists; continuing"
  fi
  log "core manifests rendered."
fi

validate_rendered_manifest

log "creating/applying secrets..."
REGENERATE_SECRETS="$REGENERATE_SECRETS" scripts/create-secrets.sh "$NAMESPACE"
log "applying core manifests..."
kubectl apply -f manifests/core/slurm-ddp-runtime.yaml
# Lmod modulefile ConfigMaps (openmpi, python3, cuda stubs).
# Declared optional in slurm-static.yaml so pods start even if applied late.
if [[ -f manifests/core/lmod-modulefiles.yaml ]]; then
  kubectl apply -f manifests/core/lmod-modulefiles.yaml
fi

# Remove obsolete single-pool resources from older layouts.
kubectl -n "$NAMESPACE" delete statefulset slurm-worker --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete service slurm-worker --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker --ignore-not-found=true >/dev/null 2>&1 || true

if [[ "$FORCE_RECREATE" == "true" ]]; then
  kubectl -n "$NAMESPACE" delete statefulset slurm-controller slurm-worker-cpu slurm-worker-gpu-a10 slurm-worker-gpu-h100 --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete service slurm-worker-cpu slurm-worker-gpu-a10 slurm-worker-gpu-h100 slurm-login --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete deployment slurm-login slurm-elastic-operator --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete pod -l app=slurm-controller --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-cpu --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-gpu-a10 --ignore-not-found=true >/dev/null 2>&1 || true
  kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-gpu-h100 --ignore-not-found=true >/dev/null 2>&1 || true
fi

kubectl apply -f manifests/core/slurm-static.yaml
if [[ -f manifests/core/slurm-login.yaml ]]; then
  kubectl apply -f manifests/core/slurm-login.yaml
fi

validate_live_commands statefulset/slurm-controller /bin/bash
validate_live_commands statefulset/slurm-worker-cpu /bin/bash

# Always restart long-lived components so /etc/munge/munge.key is recopied from projected secrets.
maybe_rollout_restart statefulset/slurm-controller
maybe_rollout_restart statefulset/slurm-worker-cpu
maybe_rollout_restart statefulset/slurm-worker-gpu-a10
maybe_rollout_restart statefulset/slurm-worker-gpu-h100
maybe_rollout_restart deployment/slurm-login
maybe_rollout_restart deployment/slurm-elastic-operator
kubectl -n "$NAMESPACE" delete pod -l app=slurm-elastic-operator --ignore-not-found=true >/dev/null 2>&1 || true
# Force fresh pods so /etc/munge/munge.key is recopied from the projected secret at process start.
kubectl -n "$NAMESPACE" delete pod -l app=slurm-controller --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-cpu --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-gpu-a10 --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-worker-gpu-h100 --ignore-not-found=true >/dev/null 2>&1 || true
kubectl -n "$NAMESPACE" delete pod -l app=slurm-login --ignore-not-found=true >/dev/null 2>&1 || true

if ! kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
  echo "namespace '$NAMESPACE' not found after apply; check current context: $(kubectl config current-context)" >&2
  exit 1
fi

controller_sts=slurm-controller
baseline_worker_sts=slurm-worker-cpu
for need in "$controller_sts" "$baseline_worker_sts"; do
  kubectl -n "$NAMESPACE" get statefulset "$need" >/dev/null 2>&1 || {
    echo "required statefulset '$need' not found in namespace '$NAMESPACE'" >&2
    kubectl -n "$NAMESPACE" get all >&2 || true
    exit 1
  }
done

log "waiting for controller rollout..."
rollout_or_dump "statefulset/${controller_sts}"
controller_pod=$(kubectl -n "$NAMESPACE" get pod -l app=slurm-controller -o jsonpath='{.items[0].metadata.name}')
wait_slurm_ready "$controller_pod"

log "waiting for baseline worker rollout (${baseline_worker_sts})..."
rollout_or_dump "statefulset/${baseline_worker_sts}"

log "building operator image..."
docker build "${build_flags[@]}" -t slurm-elastic-operator:latest -f docker/operator/Dockerfile .

log "loading operator image to kind..."
kind load docker-image slurm-elastic-operator:latest --name "$CLUSTER_NAME"

log "applying operator manifest..."
kubectl apply -f manifests/operator/slurm-elastic-operator.yaml
kubectl apply -f manifests/networking/network-policy.yaml
operator_force_env
validate_live_operator_config
kubectl -n "$NAMESPACE" delete pod -l app=slurm-elastic-operator --ignore-not-found=true >/dev/null 2>&1 || true

# Keep a single baseline worker at start so scale-up paths are observable.
kubectl -n "$NAMESPACE" scale statefulset/${baseline_worker_sts} --replicas=1 >/dev/null 2>&1 || true
for extra in slurm-worker-gpu-a10 slurm-worker-gpu-h100; do
  kubectl -n "$NAMESPACE" get statefulset "$extra" >/dev/null 2>&1 && kubectl -n "$NAMESPACE" scale statefulset/${extra} --replicas=0 >/dev/null 2>&1 || true
done

rollout_or_dump deployment/slurm-elastic-operator
rollout_or_dump "statefulset/${baseline_worker_sts}"

if kubectl -n "$NAMESPACE" get deployment slurm-login >/dev/null 2>&1; then
  rollout_or_dump deployment/slurm-login
  if ! kubectl -n "$NAMESPACE" exec deploy/slurm-login -- bash -lc 'scontrol ping >/dev/null 2>&1'; then
    echo "[bootstrap] warning: slurm-login cannot yet reach slurmctld" >&2
    kubectl -n "$NAMESPACE" logs deploy/slurm-login --tail=100 >&2 || true
  fi
fi

log "done. cluster deployed on context: ${KUBE_CONTEXT}"
