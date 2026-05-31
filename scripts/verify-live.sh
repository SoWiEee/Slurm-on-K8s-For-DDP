#!/usr/bin/env bash
# verify-live.sh - One-shot verification for the Linux + k3s + GPU live cluster.
#
# Run after scripts/deploy-1.sh and scripts/deploy-2.sh.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="${NAMESPACE:-slurm}"
MON_NAMESPACE="${MON_NAMESPACE:-monitoring}"
GPU_OPERATOR_NAMESPACE="${GPU_OPERATOR_NAMESPACE:-gpu-operator}"
KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"
VALUES_FILE="${VALUES_FILE:-chart/values-k3s.yaml}"
HELM_RELEASE="${HELM_RELEASE:-slurm-platform}"
ROLLOUT_TIMEOUT="${ROLLOUT_TIMEOUT:-300s}"
PF_WAIT="${PF_WAIT:-3}"
SKIP_HELM_RENDER="${SKIP_HELM_RENDER:-0}"
SKIP_STORAGE="${SKIP_STORAGE:-0}"
SKIP_GPU="${SKIP_GPU:-0}"
SKIP_MONITORING="${SKIP_MONITORING:-0}"
SKIP_DSAC_SMOKE="${SKIP_DSAC_SMOKE:-0}"
PARTITION="${PARTITION:-cpu}"
GPU_POOL_STS="${GPU_POOL_STS:-slurm-worker-gpu-rtx4070}"
GPU_PARTITION="${GPU_PARTITION:-gpu-rtx4070}"
GPU_CONSTRAINT="${GPU_CONSTRAINT:-gpu-rtx4070}"
GPU_GRES="${GPU_GRES:-gpu:rtx4070:1}"
GPU_WAKE_TIMEOUT="${GPU_WAKE_TIMEOUT:-180}"
export KUBECONFIG

PASS=0
FAIL=0
PORT_FORWARD_PIDS=()

log() { printf '[%(%Y-%m-%dT%H:%M:%S%z)T] [verify-live] %s\n' -1 "$*"; }
pass() { printf '  [PASS] %s\n' "$*"; PASS=$((PASS + 1)); }
fail() { printf '  [FAIL] %s\n' "$*" >&2; FAIL=$((FAIL + 1)); }
fatal() { printf '[%(%Y-%m-%dT%H:%M:%S%z)T] [verify-live][ERROR] %s\n' -1 "$*" >&2; exit 1; }
warn() { printf '[%(%Y-%m-%dT%H:%M:%S%z)T] [verify-live][WARN] %s\n' -1 "$*" >&2; }

cleanup() {
  local pid
  for pid in "${PORT_FORWARD_PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || fatal "required command not found: $1"
}

record_cmd() {
  local desc="$1"
  shift
  if "$@" >/tmp/verify-live.out 2>/tmp/verify-live.err; then
    pass "$desc"
  else
    fail "$desc"
    sed 's/^/    /' /tmp/verify-live.err >&2 || true
  fi
}

wait_seconds() {
  local t="$1"
  if [[ "$t" =~ ^[0-9]+s$ ]]; then echo "${t%s}"; return; fi
  if [[ "$t" =~ ^[0-9]+m$ ]]; then echo "$(( ${t%m} * 60 ))"; return; fi
  echo 300
}

wait_statefulset_ready() {
  local name="$1" ns="${2:-$NAMESPACE}"
  local timeout_s deadline replicas ready
  timeout_s=$(wait_seconds "$ROLLOUT_TIMEOUT")
  deadline=$(( $(date +%s) + timeout_s ))
  while true; do
    replicas=$(kubectl -n "$ns" get statefulset "$name" -o jsonpath='{.spec.replicas}' 2>/dev/null || echo 0)
    ready=$(kubectl -n "$ns" get statefulset "$name" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
    replicas=${replicas:-0}
    ready=${ready:-0}
    if [[ "$ready" == "$replicas" ]]; then
      pass "statefulset ${ns}/${name} ready ${ready}/${replicas}"
      return
    fi
    if (( $(date +%s) >= deadline )); then
      fail "statefulset ${ns}/${name} ready ${ready}/${replicas}"
      return
    fi
    sleep 3
  done
}

wait_rollout() {
  local kind="$1" name="$2" ns="${3:-$NAMESPACE}"
  if ! kubectl -n "$ns" get "$kind/$name" >/dev/null 2>&1; then
    fail "missing ${ns}/${kind}/${name}"
    return
  fi
  if [[ "$kind" == "statefulset" ]]; then
    wait_statefulset_ready "$name" "$ns"
  else
    record_cmd "rollout ${ns}/${kind}/${name}" \
      kubectl -n "$ns" rollout status "$kind/$name" --timeout="$ROLLOUT_TIMEOUT"
  fi
}

pod_ready() {
  local ns="$1" selector="$2" desc="$3"
  local ready
  ready=$(kubectl -n "$ns" get pod -l "$selector" \
    -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  if [[ "$ready" == "True" ]]; then
    pass "$desc pod is Ready"
  else
    fail "$desc pod is not Ready (${ready:-not found})"
  fi
}

login_pod() {
  kubectl -n "$NAMESPACE" get pod -l app=slurm-login -o jsonpath='{.items[0].metadata.name}'
}

login_exec() {
  local pod
  pod=$(login_pod)
  kubectl -n "$NAMESPACE" exec "pod/${pod}" -- bash -lc "$1"
}

start_port_forward() {
  local ns="$1" svc="$2" local_port="$3" remote_port="$4"
  kubectl -n "$ns" port-forward "svc/${svc}" "${local_port}:${remote_port}" >/tmp/verify-live-pf-${svc}.log 2>&1 &
  local pid=$!
  PORT_FORWARD_PIDS+=("$pid")
  sleep "$PF_WAIT"
}

http_contains() {
  local url="$1" pattern="$2" desc="$3"
  if curl -fsS --max-time 10 "$url" 2>/dev/null | grep -q "$pattern"; then
    pass "$desc"
  else
    fail "$desc"
  fi
}

check_preflight() {
  log "preflight"
  require_cmd kubectl
  require_cmd helm
  require_cmd curl
  [[ -r "$KUBECONFIG" ]] || fatal "KUBECONFIG is not readable: $KUBECONFIG"
  record_cmd "k3s node reachable" kubectl get nodes -o wide
  if kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}' | grep -q $'\tTrue'; then
    pass "at least one Kubernetes node is Ready"
  else
    fail "no Kubernetes Ready node"
  fi
}

check_helm_render() {
  [[ "$SKIP_HELM_RENDER" == "1" ]] && { warn "SKIP_HELM_RENDER=1; skipping chart render checks"; return; }
  log "helm render"
  record_cmd "helm lint values-k3s" helm lint "$ROOT_DIR/chart" -f "$ROOT_DIR/$VALUES_FILE"
  record_cmd "helm template values-k3s" helm template "$HELM_RELEASE" "$ROOT_DIR/chart" -f "$ROOT_DIR/$VALUES_FILE" -n "$NAMESPACE"
}

check_rollouts() {
  log "core rollouts"
  wait_rollout statefulset slurm-controller
  wait_rollout statefulset slurm-worker-cpu
  wait_rollout deployment slurm-login
  wait_rollout deployment slurm-elastic-operator
  wait_rollout deployment slurm-exporter
  wait_rollout deployment rl-scheduler

  log "monitoring rollouts"
  wait_rollout deployment prometheus "$MON_NAMESPACE"
  wait_rollout deployment grafana "$MON_NAMESPACE"
  wait_rollout deployment alertmanager "$MON_NAMESPACE"
  wait_rollout deployment kube-state-metrics "$MON_NAMESPACE"

  log "gpu operator presence"
  if kubectl get ns "$GPU_OPERATOR_NAMESPACE" >/dev/null 2>&1; then
    pass "gpu-operator namespace exists"
    kubectl -n "$GPU_OPERATOR_NAMESPACE" get pods >/tmp/verify-live-gpu-operator-pods.txt 2>&1 && pass "gpu-operator pods are listable" || fail "gpu-operator pods are not listable"
  else
    fail "gpu-operator namespace missing"
  fi
}

check_storage() {
  [[ "$SKIP_STORAGE" == "1" ]] && { warn "SKIP_STORAGE=1; skipping storage checks"; return; }
  log "storage"
  record_cmd "StorageClass slurm-shared-nfs exists" kubectl get storageclass slurm-shared-nfs
  local phase
  phase=$(kubectl -n "$NAMESPACE" get pvc slurm-shared-rwx -o jsonpath='{.status.phase}' 2>/dev/null || true)
  [[ "$phase" == "Bound" ]] && pass "PVC slurm-shared-rwx is Bound" || fail "PVC slurm-shared-rwx is not Bound (${phase:-missing})"

  local marker="verify-live-$(date +%s)"
  record_cmd "controller can write /shared" kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- sh -lc "printf '%s\n' '$marker' > /shared/.verify-live"
  record_cmd "cpu worker can read /shared" kubectl -n "$NAMESPACE" exec pod/slurm-worker-cpu-0 -- sh -lc "grep -Fqx '$marker' /shared/.verify-live"
  local lp
  lp=$(login_pod 2>/dev/null || true)
  [[ -n "$lp" ]] && record_cmd "login can read /shared" kubectl -n "$NAMESPACE" exec "pod/$lp" -- sh -lc "grep -Fqx '$marker' /shared/.verify-live" || fail "login pod not found"
}

wait_for_gpu_pod() {
  local deadline pod
  deadline=$(( $(date +%s) + GPU_WAKE_TIMEOUT ))
  while true; do
    pod=$(kubectl -n "$NAMESPACE" get pod -l "app=${GPU_POOL_STS}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -n "$pod" ]]; then
      printf '%s\n' "$pod"
      return 0
    fi
    (( $(date +%s) >= deadline )) && return 1
    sleep 3
  done
}

check_gpu() {
  [[ "$SKIP_GPU" == "1" ]] && { warn "SKIP_GPU=1; skipping GPU checks"; return; }
  log "gpu"
  local gpu_total
  gpu_total=$(kubectl get nodes -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null | awk 'NF{s+=$1} END{print s+0}')
  [[ "${gpu_total:-0}" -gt 0 ]] && pass "cluster advertises ${gpu_total} GPU(s)" || fail "cluster advertises no nvidia.com/gpu capacity"

  login_exec "sinfo -t drain,drained -N --noheader -o '%N' 2>/dev/null | xargs -r -I{} scontrol update nodename={} state=resume 2>/dev/null || true" >/dev/null 2>&1 || true

  local gpu_jobid
  if gpu_jobid=$(login_exec "sbatch --parsable --job-name='gpu-live-smoke' -p ${GPU_PARTITION} --constraint=${GPU_CONSTRAINT} --gres=${GPU_GRES} --wrap='nvidia-smi --query-gpu=name --format=csv,noheader >/shared/gpu-live-smoke-${RANDOM}.out'" 2>/tmp/verify-live-gpu-sbatch.err | tr -d '\r' | tail -n1); then
    pass "submitted GPU smoke job ${gpu_jobid}"
  else
    fail "failed to submit GPU smoke job"
    sed 's/^/    /' /tmp/verify-live-gpu-sbatch.err >&2 || true
  fi

  local gpu_pod
  if gpu_pod=$(wait_for_gpu_pod); then
    pass "GPU worker pod exists: ${gpu_pod}"
    record_cmd "nvidia-smi works in ${gpu_pod}" kubectl -n "$NAMESPACE" exec "pod/$gpu_pod" -- nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
  else
    fail "GPU worker pod not found for app=${GPU_POOL_STS} after ${GPU_WAKE_TIMEOUT}s"
  fi

  if login_exec "sinfo --Node --Format=Gres --noheader | grep -q gpu" >/tmp/verify-live-sinfo-gpu.txt 2>&1; then
    pass "Slurm sinfo shows GPU GRES"
  else
    fail "Slurm sinfo does not show GPU GRES"
  fi
}

check_monitoring() {
  [[ "$SKIP_MONITORING" == "1" ]] && { warn "SKIP_MONITORING=1; skipping monitoring checks"; return; }
  log "monitoring"
  pod_ready "$MON_NAMESPACE" app=prometheus prometheus
  pod_ready "$MON_NAMESPACE" app=grafana grafana
  pod_ready "$MON_NAMESPACE" app=alertmanager alertmanager
  pod_ready "$MON_NAMESPACE" app=kube-state-metrics kube-state-metrics
  pod_ready "$NAMESPACE" app=slurm-exporter slurm-exporter
  pod_ready "$NAMESPACE" app=slurm-elastic-operator slurm-elastic-operator

  start_port_forward "$MON_NAMESPACE" prometheus 19090 9090
  http_contains http://localhost:19090/-/ready 'Ready' "Prometheus /-/ready"
  for job in slurm-exporter slurm-operator kube-state-metrics rl-scheduler; do
    http_contains http://localhost:19090/api/v1/targets "\"job\":\"${job}\"" "Prometheus has target ${job}"
  done

  start_port_forward "$MON_NAMESPACE" grafana 13000 3000
  http_contains http://localhost:13000/api/health 'database' "Grafana health endpoint"
}

check_dsac_smoke() {
  [[ "$SKIP_DSAC_SMOKE" == "1" ]] && { warn "SKIP_DSAC_SMOKE=1; skipping DSAC smoke"; return; }
  log "DSAC live smoke"
  record_cmd "rl-scheduler healthz from controller" \
    kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- curl -fsS http://rl-scheduler:8002/healthz

  record_cmd "push minimal DSAC snapshot" \
    kubectl -n "$NAMESPACE" exec pod/slurm-controller-0 -- curl -fsS -X POST http://rl-scheduler:8002/snapshot \
      -H 'Content-Type: application/json' \
      -d '{"now":0,"pending_jobs":[],"nodes":[{"gpus":[{"free_mps":100,"running_jobs":0,"gpu_type":"rtx4070"}]}],"n_nodes":1,"gpus_per_node":1,"mps_per_gpu":100}'

  local jobid
  if jobid=$(login_exec "sbatch --parsable --wrap='sleep 3' --job-name='dsac-live-smoke' -p ${PARTITION}" 2>/tmp/verify-live-sbatch.err | tr -d '\r' | tail -n1); then
    pass "submitted DSAC smoke job ${jobid}"
  else
    fail "failed to submit DSAC smoke job"
    sed 's/^/    /' /tmp/verify-live-sbatch.err >&2 || true
  fi

  local deadline
  deadline=$(( $(date +%s) + 20 ))
  while true; do
    if kubectl -n "$NAMESPACE" logs slurm-controller-0 --tail=500 | grep -Eq '\[rl\]|\[score-m3\]'; then
      pass "controller logs include scheduler decision markers"
      return
    fi
    if kubectl -n "$NAMESPACE" logs deploy/rl-scheduler --tail=200 | grep -q 'POST /decide'; then
      pass "rl-scheduler logs include /decide request"
      return
    fi
    if (( $(date +%s) >= deadline )); then
      fail "no scheduler decision marker found in controller or rl-scheduler logs"
      return
    fi
    sleep 2
  done
}

summary() {
  echo ""
  log "results: ${PASS} passed, ${FAIL} failed"
  if (( FAIL > 0 )); then
    log "useful diagnostics:"
    log "  kubectl -n ${NAMESPACE} get pods -o wide"
    log "  kubectl -n ${NAMESPACE} logs deploy/rl-scheduler --tail=100"
    log "  kubectl -n ${MON_NAMESPACE} get pods -o wide"
    log "  kubectl -n ${GPU_OPERATOR_NAMESPACE} get pods -o wide"
    exit 1
  fi
}

main() {
  cd "$ROOT_DIR"
  check_preflight
  check_helm_render
  check_rollouts
  check_storage
  check_gpu
  check_monitoring
  check_dsac_smoke
  summary
}

main "$@"
