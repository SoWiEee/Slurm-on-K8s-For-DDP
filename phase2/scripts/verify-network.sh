#!/usr/bin/env bash
set -euo pipefail

NAMESPACE=${NAMESPACE:-slurm}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-slurm-lab}
MODE=${MODE:-auto}
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
TOPOLOGY_FILE=${TOPOLOGY_FILE:-$ROOT_DIR/phase2/manifests/slurm-phaseE-topology.yaml}
REQUIRE_DATA_PATH=${REQUIRE_DATA_PATH:-true}

log() {
  echo "[verify-network] $*"
}

die() {
  echo "[verify-network][ERROR] $*" >&2
  exit 1
}

require_tool() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required"
}

require_tool kubectl
if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  die "kubectl context ${KUBE_CONTEXT} not found"
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

log "topology summary"
if kubectl -n "$NAMESPACE" get configmap slurm-topology >/dev/null 2>&1; then
  kubectl -n "$NAMESPACE" get configmap slurm-topology -o go-template='{{index .data "topology.json"}}' || true
elif [[ -f "$TOPOLOGY_FILE" ]]; then
  sed -n '/topology.json: |/,$p' "$TOPOLOGY_FILE" | sed '1d' | sed -E 's/^    ?//' || true
else
  die "topology source not found"
fi

echo
log "cluster snapshot"
kubectl -n "$NAMESPACE" get pods -o wide || true

CRD_PRESENT=false
if kubectl api-resources 2>/dev/null | grep -q '^network-attachment-definitions'; then
  CRD_PRESENT=true
fi

if [[ "$MODE" == "topology" ]]; then
  log "topology-only mode requested"
  exit 0
fi

echo
log "pod template annotations"
workloads=(
  "statefulset slurm-controller"
  "deployment slurm-elastic-operator"
  "deployment slurm-login"
  "statefulset slurm-worker-cpu"
  "statefulset slurm-worker-gpu-a10"
  "statefulset slurm-worker-gpu-h100"
)
for item in "${workloads[@]}"; do
  read -r kind name <<<"$item"
  if kubectl -n "$NAMESPACE" get "$kind/$name" >/dev/null 2>&1; then
    out=$(kubectl -n "$NAMESPACE" get "$kind/$name" -o 'jsonpath={.spec.template.metadata.annotations.k8s\.v1\.cni\.cncf\.io/networks}' 2>/dev/null || true)
    echo "$kind/$name: ${out:-<none>}"
  fi
done

if [[ "$CRD_PRESENT" != "true" ]]; then
  if [[ "$MODE" == "runtime" || "$REQUIRE_DATA_PATH" == "true" ]]; then
    die "Multus CRD not detected; runtime cross-network verification cannot succeed"
  fi
  log "Multus CRD not detected; runtime attachment checks skipped"
  exit 0
fi

echo
log "network attachment definitions"
kubectl -n "$NAMESPACE" get network-attachment-definitions.k8s.cni.cncf.io || true
log "slurm-data-net config"
kubectl -n "$NAMESPACE" get network-attachment-definition slurm-data-net -o go-template='{{.spec.config}}' 2>/dev/null || true
echo

select_running_pod() {
  local selector="$1"
  kubectl -n "$NAMESPACE" get pods \
    -l "$selector" \
    --field-selector=status.phase=Running \
    --sort-by=.metadata.creationTimestamp \
    -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' 2>/dev/null | tail -n1
}

print_network_status() {
  local pod="$1"
  local status
  status=$(kubectl -n "$NAMESPACE" get pod "$pod" -o 'jsonpath={.metadata.annotations.k8s\.v1\.cni\.cncf\.io/network-status}' 2>/dev/null || true)
  if [[ -z "$status" ]]; then
    echo "$pod: <no network-status annotation>"
  else
    echo "$pod: $status"
  fi
}

log "selecting running pods for runtime checks"
login_pod=$(select_running_pod 'app=slurm-login')
worker_pod=$(select_running_pod 'app=slurm-worker-cpu')
[[ -n "$login_pod" ]] || {
  log "login pod status"
  kubectl -n "$NAMESPACE" get pods -l app=slurm-login -o wide || true
  kubectl -n "$NAMESPACE" describe pods -l app=slurm-login || true
  log "recent sandbox and CNI events"
  kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -n 25 || true
  die "no Running slurm-login pod found; rollout or CNI setup is still broken"
}
[[ -n "$worker_pod" ]] || {
  log "worker pod status"
  kubectl -n "$NAMESPACE" get pods -l app=slurm-worker-cpu -o wide || true
  kubectl -n "$NAMESPACE" describe pods -l app=slurm-worker-cpu || true
  log "recent sandbox and CNI events"
  kubectl -n "$NAMESPACE" get events --sort-by=.lastTimestamp | tail -n 25 || true
  die "no Running slurm-worker-cpu pod found; rollout or CNI setup is still broken"
}

echo
log "runtime network-status view"
print_network_status "$login_pod"
print_network_status "$worker_pod"

iface_present() {
  local pod="$1"
  local iface="$2"
  kubectl -n "$NAMESPACE" exec "$pod" -- sh -lc "grep -qE '^[[:space:]]*${iface}:' /proc/net/dev" >/dev/null 2>&1
}

iface_ipv4() {
  local pod="$1"
  local iface="$2"
  kubectl -n "$NAMESPACE" exec "$pod" -- sh -lc '
    if command -v ip >/dev/null 2>&1; then
      ip -o -4 addr show dev "'$iface'" 2>/dev/null | awk "{print \$4}" | head -n1
    elif command -v ifconfig >/dev/null 2>&1; then
      ifconfig "'$iface'" 2>/dev/null | sed -n "s/.*inet \([0-9.]*\).*/\1/p" | head -n1
    fi
  ' 2>/dev/null
}

iface_ip_from_status() {
  local pod="$1"
  local status
  status=$(kubectl -n "$NAMESPACE" get pod "$pod" -o 'jsonpath={.metadata.annotations.k8s\.v1\.cni\.cncf\.io/network-status}' 2>/dev/null || true)
  [[ -n "$status" ]] || return 0
  printf '%s' "$status" \
    | tr -d '\r\n ' \
    | sed -n 's/.*"interface":"net2","ips":\["\([0-9.]*\)".*/\1/p' \
    | head -n1
}



iface_present "$login_pod" net2 || die "${login_pod} does not expose net2 inside the container"
iface_present "$worker_pod" net2 || die "${worker_pod} does not expose net2 inside the container"

login_net2=$(iface_ipv4 "$login_pod" net2 || true)
worker_net2=$(iface_ipv4 "$worker_pod" net2 || true)

if [[ -z "$login_net2" ]]; then
  log "warning: could not read net2 IPv4 from inside ${login_pod}; using network-status annotation as fallback"
  login_net2=$(iface_ip_from_status "$login_pod")
fi
if [[ -z "$worker_net2" ]]; then
  log "warning: could not read net2 IPv4 from inside ${worker_pod}; using network-status annotation as fallback"
  worker_net2=$(iface_ip_from_status "$worker_pod")
fi

[[ -n "$login_net2" ]] || die "${login_pod} exposes net2 but no IPv4 address could be determined"
[[ -n "$worker_net2" ]] || die "${worker_pod} exposes net2 but no IPv4 address could be determined"

log "net2 addresses"
echo "  ${login_pod}: ${login_net2}"
echo "  ${worker_pod}: ${worker_net2}"

log "checking runtime helper availability"
kubectl -n "$NAMESPACE" exec "$login_pod" -- bash -lc 'test -f /opt/slurm-runtime/ddp-env.sh && grep -q NCCL_SOCKET_IFNAME /opt/slurm-runtime/ddp-env.sh'
kubectl -n "$NAMESPACE" exec "$worker_pod" -- bash -lc 'test -f /opt/slurm-runtime/ddp-env.sh && grep -q GLOO_SOCKET_IFNAME /opt/slurm-runtime/ddp-env.sh'

log "probing data-plane ssh from login to worker over net2 (warning-only)"
if ! kubectl -n "$NAMESPACE" exec "$login_pod" -- bash -lc "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 root@${worker_net2%/*} 'hostname; grep -E "^[[:space:]]*net2:" /proc/net/dev || true'"; then
  log "warning: direct SSH over net2 is not configured or not reachable; continuing because SSH is optional for the MVP"
fi

log "sampling ddp env output inside login pod"
kubectl -n "$NAMESPACE" exec "$login_pod" -- bash -lc 'set -a; source /opt/slurm-runtime/ddp-env.sh >/tmp/ddp-env.out 2>&1 || true; set +a; cat /tmp/ddp-env.out; env | grep -E "^(NCCL_SOCKET_IFNAME|GLOO_SOCKET_IFNAME|SLURM_DATA_IFACE|SLURM_DATA_IP)=" | sort'

log "done"
