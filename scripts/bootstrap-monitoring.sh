#!/usr/bin/env bash
# bootstrap-monitoring.sh — Deploy the monitoring stack (Prometheus + Grafana + exporters)
#
# Prerequisites: core cluster deployed (bootstrap.sh); NFS storage optional but recommended.
# What this script does:
#   1. Verify prerequisites
#   2. Build and load slurm-exporter image into Kind
#   3. Rebuild operator image (now includes prometheus-client)
#   4. Apply monitoring namespace + kube-state-metrics
#   5. Apply Prometheus (config + deployment + service)
#   6. Apply Grafana (provisioning + dashboards + deployment)
#   7. Apply slurm-exporter in slurm namespace
#   8. Apply NetworkPolicy rules for cross-namespace scraping
#   9. Restart operator deployment so new image takes effect
#  10. Wait for all pods ready + print access instructions
set -euo pipefail

CLUSTER_NAME=${CLUSTER_NAME:-slurm-lab}
NAMESPACE=${NAMESPACE:-slurm}
MON_NAMESPACE=${MON_NAMESPACE:-monitoring}
K8S_RUNTIME=${K8S_RUNTIME:-kind}
KUBE_CONTEXT=${KUBE_CONTEXT:-$([[ "$K8S_RUNTIME" == "k3s" ]] && echo "default" || echo "kind-${CLUSTER_NAME}")}
ROLLOUT_TIMEOUT=${ROLLOUT_TIMEOUT:-300s}
DOCKER_BUILD_NO_CACHE=${DOCKER_BUILD_NO_CACHE:-false}

log() { echo "[bootstrap-monitoring] $*"; }

if ! kubectl config get-contexts -o name | grep -q "^${KUBE_CONTEXT}$"; then
  echo "kubectl context ${KUBE_CONTEXT} not found" >&2
  kubectl config get-contexts -o name >&2 || true
  exit 1
fi
kubectl config use-context "$KUBE_CONTEXT" >/dev/null

# ----- prerequisites --------------------------------------------------------
log "checking prerequisites..."
if ! kubectl -n "$NAMESPACE" get statefulset slurm-controller slurm-worker-cpu >/dev/null 2>&1; then
  echo "Cluster resources not found in namespace ${NAMESPACE}." >&2
  echo "Run scripts/bootstrap.sh first." >&2
  exit 1
fi
if ! kubectl -n "$NAMESPACE" get deployment slurm-elastic-operator >/dev/null 2>&1; then
  echo "Operator not found in namespace ${NAMESPACE}." >&2
  echo "Run scripts/bootstrap.sh first." >&2
  exit 1
fi
log "prerequisites OK."

build_flags=()
if [[ "$DOCKER_BUILD_NO_CACHE" == "true" ]]; then
  build_flags+=(--no-cache)
fi

# ----- build images ---------------------------------------------------------
log "building slurm-exporter image..."
docker build "${build_flags[@]}" \
  -t slurm-exporter:latest \
  -f docker/slurm-exporter/Dockerfile \
  docker/slurm-exporter

if [[ "$K8S_RUNTIME" != "k3s" ]]; then
  log "loading slurm-exporter image to kind..."
  kind load docker-image slurm-exporter:latest --name "$CLUSTER_NAME"
else
  log "k3s runtime — importing slurm-exporter image into containerd via 'k3s ctr images import'..."
  docker save slurm-exporter:latest | sudo k3s ctr images import -
fi

log "rebuilding operator image (adds prometheus-client)..."
docker build "${build_flags[@]}" \
  -t slurm-elastic-operator:phase2 \
  -f docker/operator/Dockerfile .

if [[ "$K8S_RUNTIME" != "k3s" ]]; then
  log "loading operator image to kind..."
  kind load docker-image slurm-elastic-operator:phase2 --name "$CLUSTER_NAME"
else
  log "k3s runtime — importing operator image into containerd via 'k3s ctr images import'..."
  docker save slurm-elastic-operator:phase2 | sudo k3s ctr images import -
fi

# ----- monitoring namespace + kube-state-metrics ----------------------------
log "applying monitoring namespace..."
kubectl apply -f manifests/monitoring/monitoring-namespace.yaml

log "applying kube-state-metrics..."
kubectl apply -f manifests/monitoring/kube-state-metrics/kube-state-metrics.yaml

# ----- prometheus ------------------------------------------------------------
log "applying prometheus alert rules..."
kubectl apply -f manifests/monitoring/prometheus/alert-rules-cm.yaml

log "applying prometheus config + deployment..."
kubectl apply -f manifests/monitoring/prometheus/prometheus-config.yaml
kubectl apply -f manifests/monitoring/prometheus/prometheus-deployment.yaml

# ----- alertmanager ----------------------------------------------------------
log "applying alertmanager..."
kubectl apply -f manifests/monitoring/alertmanager/alertmanager.yaml

# ----- grafana ---------------------------------------------------------------
log "applying grafana provisioning + dashboards + deployment..."
kubectl apply -f manifests/monitoring/grafana/grafana-provisioning-cm.yaml
kubectl apply -f manifests/monitoring/grafana/grafana-dashboards-cm.yaml
kubectl apply -f manifests/monitoring/grafana/grafana-deployment.yaml

# ----- slurm-exporter (in slurm namespace) -----------------------------------
log "applying slurm-exporter..."
kubectl apply -f manifests/monitoring/slurm-exporter/slurm-exporter-deployment.yaml

# ----- network policies (allow prometheus → slurm namespace) -----------------
log "applying monitoring network policies..."
kubectl apply -f manifests/networking/network-policy-monitoring.yaml

# ----- update operator manifest (adds metrics port + service) ----------------
log "applying updated operator manifest (metrics port + service)..."
kubectl apply -f manifests/operator/slurm-elastic-operator.yaml

# ----- restart operator so new image (with prometheus-client) is used --------
log "restarting operator deployment..."
kubectl -n "$NAMESPACE" rollout restart deployment/slurm-elastic-operator
kubectl -n "$NAMESPACE" delete pod -l app=slurm-elastic-operator --ignore-not-found=true >/dev/null 2>&1 || true

# ----- wait for rollouts -----------------------------------------------------
log "waiting for operator rollout..."
kubectl -n "$NAMESPACE" rollout status deployment/slurm-elastic-operator --timeout="$ROLLOUT_TIMEOUT"

log "waiting for slurm-exporter rollout..."
kubectl -n "$NAMESPACE" rollout status deployment/slurm-exporter --timeout="$ROLLOUT_TIMEOUT"

log "waiting for kube-state-metrics rollout..."
kubectl -n "$MON_NAMESPACE" rollout status deployment/kube-state-metrics --timeout="$ROLLOUT_TIMEOUT"

log "waiting for prometheus rollout..."
kubectl -n "$MON_NAMESPACE" rollout status deployment/prometheus --timeout="$ROLLOUT_TIMEOUT"

log "waiting for grafana rollout..."
kubectl -n "$MON_NAMESPACE" rollout status deployment/grafana --timeout="$ROLLOUT_TIMEOUT"

log "waiting for alertmanager rollout..."
kubectl -n "$MON_NAMESPACE" rollout status deployment/alertmanager --timeout="$ROLLOUT_TIMEOUT"

# ----- done ------------------------------------------------------------------
cat <<'EOF'

=======================================================
 Phase 4 monitoring stack deployed successfully.
=======================================================

Access dashboards:

  # Grafana  (admin / admin)
  kubectl -n monitoring port-forward svc/grafana 3000:3000
  → http://localhost:3000
    Dashboards → Slurm folder:
      • Slurm↔K8s Bridge Overview
      • K8s Elastic Operator
      • SLA & Efficiency

  # Prometheus (alerts / raw metrics)
  kubectl -n monitoring port-forward svc/prometheus 9090:9090
  → http://localhost:9090/alerts   ← SLO alert status

  # Alertmanager (alert routing)
  kubectl -n monitoring port-forward svc/alertmanager 9093:9093
  → http://localhost:9093

  # Verify operator metrics
  kubectl -n slurm port-forward svc/slurm-elastic-operator 8000:8000
  → curl http://localhost:8000/metrics | grep slurm_operator

  # Verify slurm-exporter metrics (includes sdiag)
  kubectl -n slurm port-forward svc/slurm-exporter 9341:9341
  → curl http://localhost:9341/metrics | grep -E 'slurm_(queue|scheduler|backfill)'

=======================================================
EOF
