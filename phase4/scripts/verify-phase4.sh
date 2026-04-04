#!/usr/bin/env bash
# verify-phase4.sh — Verify that all Phase 4 monitoring components are healthy
# and that key metrics are being scraped correctly.
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
MON_NAMESPACE=${MON_NAMESPACE:-monitoring}
KUBE_CONTEXT=${KUBE_CONTEXT:-kind-${CLUSTER_NAME}}
PF_WAIT=${PF_WAIT:-3}   # seconds to wait after starting a port-forward

PASS=0; FAIL=0

log()  { echo "[verify-phase4] $*"; }
pass() { echo "  [PASS] $*"; (( PASS++ )) || true; }
fail() { echo "  [FAIL] $*" >&2; (( FAIL++ )) || true; }

kubectl config use-context "$KUBE_CONTEXT" >/dev/null

check_pod_ready() {
  local ns="$1" label="$2" name="$3"
  local ready
  ready=$(kubectl -n "$ns" get pods -l "$label" \
    -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)
  if [[ "$ready" == "True" ]]; then
    pass "$name pod is Ready"
  else
    fail "$name pod not Ready (status=${ready:-<not found>})"
  fi
}

# check_metrics <ns> <svc> <local_port> <remote_port> <grep_pattern> <desc>
check_metrics() {
  local ns="$1" svc="$2" lport="$3" rport="$4" pattern="$5" desc="$6"
  kubectl -n "$ns" port-forward "svc/${svc}" "${lport}:${rport}" >/dev/null 2>&1 &
  local pf_pid=$!
  sleep "$PF_WAIT"
  if curl -sf --max-time 5 "http://localhost:${lport}/metrics" 2>/dev/null \
      | grep -q "$pattern"; then
    pass "$desc: found metric '${pattern}'"
  else
    fail "$desc: metric '${pattern}' not found in /metrics output"
  fi
  kill "$pf_pid" 2>/dev/null || true
  wait "$pf_pid" 2>/dev/null || true
}

log "=== Phase 4 Verification ==="

# --- Pod readiness ---
log "--- Pod readiness ---"
check_pod_ready "$MON_NAMESPACE" "app=kube-state-metrics"     "kube-state-metrics"
check_pod_ready "$MON_NAMESPACE" "app=prometheus"             "prometheus"
check_pod_ready "$MON_NAMESPACE" "app=grafana"                "grafana"
check_pod_ready "$NAMESPACE"     "app=slurm-exporter"         "slurm-exporter"
check_pod_ready "$NAMESPACE"     "app=slurm-elastic-operator" "slurm-elastic-operator"

# --- Services exist ---
log "--- Services ---"
for svc_check in \
  "${MON_NAMESPACE}/kube-state-metrics" \
  "${MON_NAMESPACE}/prometheus" \
  "${MON_NAMESPACE}/grafana" \
  "${NAMESPACE}/slurm-exporter" \
  "${NAMESPACE}/slurm-elastic-operator"; do
  ns="${svc_check%%/*}"; svc="${svc_check##*/}"
  if kubectl -n "$ns" get svc "$svc" >/dev/null 2>&1; then
    pass "Service ${svc_check} exists"
  else
    fail "Service ${svc_check} not found"
  fi
done

# --- Metrics endpoints (port-forward → host curl, no exec needed) ---
log "--- Metrics endpoints ---"
check_metrics "$NAMESPACE"     "slurm-exporter"         19341 9341 "slurm_exporter_scrape_success"   "slurm-exporter"
check_metrics "$NAMESPACE"     "slurm-elastic-operator" 19342 8000 "slurm_operator_current_replicas"  "operator metrics"
check_metrics "$MON_NAMESPACE" "kube-state-metrics"     19343 8080 "kube_statefulset_status_replicas" "kube-state-metrics"

# --- Prometheus targets ---
log "--- Prometheus target scrape status ---"
kubectl -n "$MON_NAMESPACE" port-forward svc/prometheus 19090:9090 >/dev/null 2>&1 &
PF_PROM=$!
sleep "$PF_WAIT"
targets_json=$(curl -sf --max-time 10 "http://localhost:19090/api/v1/targets" 2>/dev/null || true)
kill "$PF_PROM" 2>/dev/null || true
wait "$PF_PROM" 2>/dev/null || true

if [[ -z "$targets_json" ]]; then
  fail "Prometheus /api/v1/targets returned empty"
else
  for job in slurm-exporter slurm-operator kube-state-metrics; do
    if echo "$targets_json" | grep -q "\"job\":\"${job}\""; then
      if echo "$targets_json" | grep -A5 "\"job\":\"${job}\"" | grep -q '"health":"up"'; then
        pass "Prometheus target '${job}' is UP"
      else
        fail "Prometheus target '${job}' is DOWN or unhealthy"
      fi
    else
      fail "Prometheus target '${job}' not found in /api/v1/targets"
    fi
  done
fi

# --- Grafana health ---
log "--- Grafana health ---"
kubectl -n "$MON_NAMESPACE" port-forward svc/grafana 13000:3000 >/dev/null 2>&1 &
PF_GRAF=$!
sleep "$PF_WAIT"

health=$(curl -sf --max-time 5 "http://localhost:13000/api/health" 2>/dev/null || true)
if echo "$health" | grep -q '"database": "ok"'; then
  pass "Grafana /api/health: database OK"
else
  fail "Grafana /api/health check failed (response: ${health:0:120})"
fi

dashboards=$(curl -sf --max-time 5 \
  "http://admin:admin@localhost:13000/api/search?type=dash-db" 2>/dev/null || true)
for uid in slurm-bridge-overview slurm-k8s-operator slurm-sla-efficiency; do
  if echo "$dashboards" | grep -q "\"uid\":\"${uid}\""; then
    pass "Grafana dashboard '${uid}' provisioned"
  else
    fail "Grafana dashboard '${uid}' not found"
  fi
done

kill "$PF_GRAF" 2>/dev/null || true
wait "$PF_GRAF" 2>/dev/null || true

# --- Summary ---
echo ""
log "=== Results: ${PASS} passed, ${FAIL} failed ==="
if (( FAIL > 0 )); then
  echo ""
  log "Troubleshooting tips:"
  log "  kubectl -n ${MON_NAMESPACE} get pods -o wide"
  log "  kubectl -n ${MON_NAMESPACE} logs deployment/prometheus --tail=50"
  log "  kubectl -n ${MON_NAMESPACE} logs deployment/grafana --tail=50"
  log "  kubectl -n ${NAMESPACE} logs deployment/slurm-exporter --tail=50"
  log "  kubectl -n ${NAMESPACE} logs deployment/slurm-elastic-operator --tail=50 | python3 -m json.tool"
  exit 1
fi
