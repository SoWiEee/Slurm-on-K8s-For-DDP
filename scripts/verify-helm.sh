#!/usr/bin/env bash
# Static + dry-run verification for chart/ (Phase 5-A Helm migration).
#
# Run BEFORE applying the chart to a cluster, and BEFORE verify-gpu.sh /
# verify.sh / verify-storage.sh against a chart-installed deployment. Catches
# template errors, missing values, and schema regressions.
#
# Optional cluster check: if kubectl + KUBECONFIG can reach an API server,
# `kubectl apply --dry-run=server` validates every rendered resource.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHART_DIR="${CHART_DIR:-${ROOT}/chart}"
RELEASE_NAME="${RELEASE_NAME:-slurm-platform}"
NAMESPACE="${NAMESPACE:-slurm}"
SKIP_CLUSTER_DRYRUN="${SKIP_CLUSTER_DRYRUN:-}"

WORKDIR="$(mktemp -d -t verify-helm.XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT

log()  { echo "[verify-helm] $*"; }
warn() { echo "[verify-helm][WARN] $*" >&2; }
fail() { echo "[verify-helm][ERROR] $*" >&2; exit 1; }

# --- 0. preflight -----------------------------------------------------------
command -v helm >/dev/null || fail "helm not found in PATH"
[[ -d "$CHART_DIR" ]] || fail "chart dir not found: $CHART_DIR"
[[ -f "$CHART_DIR/Chart.yaml" ]] || fail "Chart.yaml missing in $CHART_DIR"
HELM_VER="$(helm version --short)"
log "helm: $HELM_VER"
log "chart: $CHART_DIR"

declare -a OVERLAYS=()
[[ -f "$CHART_DIR/values-k3s.yaml" ]] && OVERLAYS+=("values-k3s.yaml")
[[ -f "$CHART_DIR/values-dev.yaml" ]] && OVERLAYS+=("values-dev.yaml")

# --- 1. helm lint -----------------------------------------------------------
log "lint: default values"
helm lint "$CHART_DIR" >/dev/null || fail "helm lint failed (default values)"
for ov in "${OVERLAYS[@]}"; do
  log "lint: -f $ov"
  helm lint "$CHART_DIR" -f "$CHART_DIR/$ov" >/dev/null \
    || fail "helm lint failed (-f $ov)"
done

# --- 2. helm template (renders) --------------------------------------------
render_default="$WORKDIR/render-default.yaml"
log "template: default values → $render_default"
helm template "$RELEASE_NAME" "$CHART_DIR" -n "$NAMESPACE" \
  > "$render_default" 2>"$WORKDIR/render-default.err" \
  || { cat "$WORKDIR/render-default.err" >&2; fail "helm template failed (default)"; }
[[ -s "$render_default" ]] || fail "helm template default produced empty output"

declare -A RENDERS=( ["default"]="$render_default" )
for ov in "${OVERLAYS[@]}"; do
  out="$WORKDIR/render-${ov%.yaml}.yaml"
  log "template: -f $ov → $out"
  helm template "$RELEASE_NAME" "$CHART_DIR" -f "$CHART_DIR/$ov" -n "$NAMESPACE" \
    > "$out" 2>"$WORKDIR/${ov%.yaml}.err" \
    || { cat "$WORKDIR/${ov%.yaml}.err" >&2; fail "helm template failed (-f $ov)"; }
  RENDERS["$ov"]="$out"
done

# --- 3. resource summary ---------------------------------------------------
for label in "${!RENDERS[@]}"; do
  log "resources ($label):"
  grep -E '^kind:' "${RENDERS[$label]}" | sort | uniq -c | sed 's/^/  /'
done

# Extract one resource (by kind + metadata.name) from a multi-doc YAML to stdout.
# Robust to YAML having multiple `name:` fields per resource (we want
# metadata.name only — the first `  name:` after each `metadata:`).
extract_resource() {
  local input="$1" want_kind="$2" want_name="$3"
  awk -v wk="$want_kind" -v wn="$want_name" '
    function emit() {
      if (kind == wk && name == wn) printf "%s", buf
      buf = ""; kind = ""; name = ""; meta = 0
    }
    /^---$/ { emit(); next }
    { buf = buf $0 "\n" }
    /^kind:/ { kind = $2 }
    /^metadata:/ { meta = 1 }
    meta && /^  name:/ { if (name == "") name = $2; meta = 0 }
    END { emit() }
  ' "$input"
}

# --- 4. semantic spot checks (k3s overlay only) ----------------------------
if [[ -n "${RENDERS[values-k3s.yaml]:-}" ]]; then
  k3s_render="${RENDERS[values-k3s.yaml]}"

  log "spot-check: GPU pool runtime + nvidia.com/gpu (k3s overlay)"
  extract_resource "$k3s_render" StatefulSet slurm-worker-gpu-rtx4070 \
    > "$WORKDIR/sts-rtx4070.yaml"
  [[ -s "$WORKDIR/sts-rtx4070.yaml" ]] \
    || fail "could not extract StatefulSet/slurm-worker-gpu-rtx4070"
  grep -q 'runtimeClassName: nvidia' "$WORKDIR/sts-rtx4070.yaml" \
    || fail "rtx4070 StatefulSet missing runtimeClassName: nvidia"
  grep -q 'nvidia.com/gpu' "$WORKDIR/sts-rtx4070.yaml" \
    || fail "rtx4070 StatefulSet missing nvidia.com/gpu resource limit"

  log "spot-check: CPU pool MUST NOT have nvidia runtime"
  extract_resource "$k3s_render" StatefulSet slurm-worker-cpu \
    > "$WORKDIR/sts-cpu.yaml"
  [[ -s "$WORKDIR/sts-cpu.yaml" ]] \
    || fail "could not extract StatefulSet/slurm-worker-cpu"
  if grep -qE 'runtimeClassName: nvidia|nvidia\.com/gpu' "$WORKDIR/sts-cpu.yaml"; then
    fail "CPU pool StatefulSet should not have nvidia runtime/resources"
  fi

  log "spot-check: controller has jwt secret in projected volume"
  extract_resource "$k3s_render" StatefulSet slurm-controller \
    > "$WORKDIR/sts-controller.yaml"
  [[ -s "$WORKDIR/sts-controller.yaml" ]] \
    || fail "could not extract StatefulSet/slurm-controller"
  grep -q 'name: slurm-jwt-secret' "$WORKDIR/sts-controller.yaml" \
    || fail "controller missing jwt secret projection"

  log "spot-check: worker pods MUST NOT mount jwt secret"
  for sts in slurm-worker-cpu slurm-worker-gpu-rtx4070 slurm-worker-gpu-rtx4080; do
    extract_resource "$k3s_render" StatefulSet "$sts" > "$WORKDIR/sts-${sts}.yaml"
    if grep -q 'name: slurm-jwt-secret' "$WORKDIR/sts-${sts}.yaml"; then
      fail "$sts StatefulSet should not project jwt secret (controller-only)"
    fi
  done

  # ----- Stage C+ checks: operator + login + NetworkPolicy ------------------
  if extract_resource "$k3s_render" Deployment slurm-elastic-operator \
      > "$WORKDIR/dep-operator.yaml" && [[ -s "$WORKDIR/dep-operator.yaml" ]]; then
    log "spot-check: operator has PARTITIONS_JSON env covering all pools"
    grep -q 'name: PARTITIONS_JSON' "$WORKDIR/dep-operator.yaml" \
      || fail "operator missing PARTITIONS_JSON env"
    for pool in cpu gpu-rtx4070 gpu-rtx4080; do
      grep -q "\"partition\":\"$pool\"" "$WORKDIR/dep-operator.yaml" \
        || fail "operator PARTITIONS_JSON missing partition '$pool'"
    done
    log "spot-check: operator mounts slurm-jwt-secret"
    grep -q 'secretName: slurm-jwt-secret' "$WORKDIR/dep-operator.yaml" \
      || fail "operator missing slurm-jwt-secret volume"
  fi

  if extract_resource "$k3s_render" Deployment slurm-login \
      > "$WORKDIR/dep-login.yaml" && [[ -s "$WORKDIR/dep-login.yaml" ]]; then
    log "spot-check: login MUST NOT mount jwt secret"
    if grep -q 'name: slurm-jwt-secret' "$WORKDIR/dep-login.yaml"; then
      fail "login Deployment should not project jwt secret (controller+operator only)"
    fi
    log "spot-check: login mounts slurm-ddp-runtime ConfigMap"
    grep -q 'name: slurm-ddp-runtime' "$WORKDIR/dep-login.yaml" \
      || fail "login missing slurm-ddp-runtime ConfigMap mount"
  fi

  np_count=$(grep -cE '^kind: NetworkPolicy$' "$k3s_render" || true)
  if (( np_count > 0 )); then
    log "spot-check: NetworkPolicy operator egress allows both 443 and 6443"
    extract_resource "$k3s_render" NetworkPolicy allow-operator-egress \
      > "$WORKDIR/np-operator.yaml"
    grep -q 'port: 443' "$WORKDIR/np-operator.yaml" \
      || fail "operator NetworkPolicy missing API server port 443 (Kind)"
    grep -q 'port: 6443' "$WORKDIR/np-operator.yaml" \
      || fail "operator NetworkPolicy missing API server port 6443 (k3s)"
  fi

  # ----- Stage D checks: device-plugin-config + GPU labeler ---------------
  if grep -q 'name: slurm-platform-device-plugin-config' "$k3s_render"; then
    log "spot-check: device-plugin-config has rtx4070-mps with sharing.mps replicas=4"
    extract_resource "$k3s_render" ConfigMap slurm-platform-device-plugin-config \
      > "$WORKDIR/cm-device-plugin.yaml"
    grep -q 'rtx4070-mps:' "$WORKDIR/cm-device-plugin.yaml" \
      || fail "device-plugin-config missing rtx4070-mps key"
    grep -q 'replicas: 4' "$WORKDIR/cm-device-plugin.yaml" \
      || fail "device-plugin-config rtx4070-mps missing sharing.mps replicas=4"

    log "spot-check: device-plugin-config lives in gpu-operator namespace"
    grep -q 'namespace: gpu-operator' "$WORKDIR/cm-device-plugin.yaml" \
      || fail "device-plugin-config not in gpu-operator namespace"

    log "spot-check: gpu-operator Namespace has PSS=privileged"
    extract_resource "$k3s_render" Namespace gpu-operator > "$WORKDIR/ns-gpu.yaml"
    [[ -s "$WORKDIR/ns-gpu.yaml" ]] \
      || fail "gpu-operator Namespace not rendered (gpu.enabled but namespace missing)"
    grep -q 'pod-security.kubernetes.io/enforce: privileged' "$WORKDIR/ns-gpu.yaml" \
      || fail "gpu-operator Namespace not labeled PSS=privileged"
  fi

  if extract_resource "$k3s_render" Job slurm-platform-gpu-labeler \
      > "$WORKDIR/job-labeler.yaml" && [[ -s "$WORKDIR/job-labeler.yaml" ]]; then
    log "spot-check: gpu-labeler Job has post-install hook annotation"
    grep -q '"helm.sh/hook": post-install' "$WORKDIR/job-labeler.yaml" \
      || fail "gpu-labeler Job missing helm.sh/hook=post-install"
  fi

  # ----- Stage E checks: monitoring + storage --------------------------------
  if extract_resource "$k3s_render" Namespace monitoring \
      > "$WORKDIR/ns-mon.yaml" && [[ -s "$WORKDIR/ns-mon.yaml" ]]; then
    log "spot-check: monitoring Namespace has kubernetes.io/metadata.name label"
    grep -q 'kubernetes.io/metadata.name: monitoring' "$WORKDIR/ns-mon.yaml" \
      || fail "monitoring Namespace missing kubernetes.io/metadata.name label (cross-ns NetworkPolicy depends on it)"

    log "spot-check: prometheus scrapes operator + slurm-exporter + kube-state-metrics"
    extract_resource "$k3s_render" ConfigMap prometheus-config \
      > "$WORKDIR/cm-prom.yaml"
    grep -q 'job_name: slurm-exporter' "$WORKDIR/cm-prom.yaml" \
      || fail "prometheus-config missing slurm-exporter scrape job"
    grep -q 'job_name: slurm-operator' "$WORKDIR/cm-prom.yaml" \
      || fail "prometheus-config missing slurm-operator scrape job"
    grep -q 'job_name: kube-state-metrics' "$WORKDIR/cm-prom.yaml" \
      || fail "prometheus-config missing kube-state-metrics scrape job"

    log "spot-check: prometheus alertmanager target points at monitoring namespace"
    grep -q "alertmanager.monitoring.svc.cluster.local:9093" "$WORKDIR/cm-prom.yaml" \
      || fail "prometheus-config alertmanager target not in monitoring namespace"

    log "spot-check: prometheus-alert-rules preserves Prometheus templating syntax"
    extract_resource "$k3s_render" ConfigMap prometheus-alert-rules \
      > "$WORKDIR/cm-prom-rules.yaml"
    # The {{ "{{ $value }}" }} escape must produce literal {{ $value }} in output
    grep -q '{{ \$value | printf' "$WORKDIR/cm-prom-rules.yaml" \
      || fail "prometheus-alert-rules lost Go template escape (Prometheus needs literal {{ \$value }})"

    log "spot-check: alertmanager defaults to dev-null receiver"
    extract_resource "$k3s_render" ConfigMap alertmanager-config \
      > "$WORKDIR/cm-am.yaml"
    grep -q 'receiver: dev-null' "$WORKDIR/cm-am.yaml" \
      || fail "alertmanager-config missing dev-null receiver (default)"
    if grep -q 'slack_configs:' "$WORKDIR/cm-am.yaml"; then
      fail "alertmanager-config has slack receiver but webhookUrl is unset (k3s overlay)"
    fi

    log "spot-check: grafana datasource points at monitoring/prometheus"
    extract_resource "$k3s_render" ConfigMap grafana-provisioning \
      > "$WORKDIR/cm-graf-prov.yaml"
    grep -q "url: http://prometheus.monitoring.svc.cluster.local:9090" "$WORKDIR/cm-graf-prov.yaml" \
      || fail "grafana-provisioning datasource URL wrong"

    log "spot-check: grafana-dashboards ConfigMap loaded all 3 dashboards"
    extract_resource "$k3s_render" ConfigMap grafana-dashboards \
      > "$WORKDIR/cm-graf-dash.yaml"
    for d in bridge-overview operator sla-efficiency; do
      grep -qE "^  ${d}\\.json: \\|" "$WORKDIR/cm-graf-dash.yaml" \
        || fail "grafana-dashboards missing ${d}.json"
    done

    log "spot-check: slurm-exporter Deployment lives in slurm namespace + mounts JWT"
    extract_resource "$k3s_render" Deployment slurm-exporter \
      > "$WORKDIR/dep-exporter.yaml"
    grep -q 'namespace: slurm' "$WORKDIR/dep-exporter.yaml" \
      || fail "slurm-exporter Deployment not in slurm namespace"
    grep -q 'secretName: slurm-jwt-secret' "$WORKDIR/dep-exporter.yaml" \
      || fail "slurm-exporter missing slurm-jwt-secret volume"

    log "spot-check: kube-state-metrics ClusterRole limited to slurm-relevant resources"
    extract_resource "$k3s_render" ClusterRole slurm-platform-kube-state-metrics \
      > "$WORKDIR/cr-ksm.yaml"
    [[ -s "$WORKDIR/cr-ksm.yaml" ]] \
      || fail "kube-state-metrics ClusterRole not rendered"

    log "spot-check: NetworkPolicy allow-prometheus-scrape-{operator,exporter} present"
    for p in allow-prometheus-scrape-operator allow-prometheus-scrape-exporter; do
      extract_resource "$k3s_render" NetworkPolicy "$p" > "$WORKDIR/np-${p}.yaml"
      [[ -s "$WORKDIR/np-${p}.yaml" ]] \
        || fail "NetworkPolicy $p missing (monitoring scrape blocked by default-deny)"
      grep -q 'kubernetes.io/metadata.name: monitoring' "$WORKDIR/np-${p}.yaml" \
        || fail "NetworkPolicy $p doesn't allow ingress from monitoring namespace"
    done

    log "spot-check: NetworkPolicy allow-slurm-exporter-egress reaches controller:6820"
    extract_resource "$k3s_render" NetworkPolicy allow-slurm-exporter-egress \
      > "$WORKDIR/np-exporter-egress.yaml"
    grep -q 'app: slurm-controller' "$WORKDIR/np-exporter-egress.yaml" \
      || fail "slurm-exporter egress missing slurm-controller target"
    grep -q 'port: 6820' "$WORKDIR/np-exporter-egress.yaml" \
      || fail "slurm-exporter egress missing port 6820"
  fi

  if extract_resource "$k3s_render" StorageClass slurm-shared-nfs \
      > "$WORKDIR/sc-nfs.yaml" && [[ -s "$WORKDIR/sc-nfs.yaml" ]]; then
    log "spot-check: slurm-shared-nfs StorageClass uses nfs-subdir provisioner + Retain"
    grep -q 'reclaimPolicy: Retain' "$WORKDIR/sc-nfs.yaml" \
      || fail "StorageClass slurm-shared-nfs reclaimPolicy != Retain"
    grep -q 'provisioner: k8s-sigs.io/slurm-nfs-subdir-external-provisioner' "$WORKDIR/sc-nfs.yaml" \
      || fail "StorageClass slurm-shared-nfs has wrong provisioner name (must match legacy bootstrap-storage.sh)"
    grep -q '"helm.sh/resource-policy": keep' "$WORKDIR/sc-nfs.yaml" \
      || fail "StorageClass slurm-shared-nfs missing helm.sh/resource-policy=keep"

    log "spot-check: slurm-shared-rwx PVC in slurm namespace, RWX, references SC"
    extract_resource "$k3s_render" PersistentVolumeClaim slurm-shared-rwx \
      > "$WORKDIR/pvc-shared.yaml"
    grep -q 'namespace: slurm' "$WORKDIR/pvc-shared.yaml" \
      || fail "slurm-shared-rwx PVC not in slurm namespace"
    grep -q 'ReadWriteMany' "$WORKDIR/pvc-shared.yaml" \
      || fail "slurm-shared-rwx PVC not RWX"
    grep -q 'storageClassName: slurm-shared-nfs' "$WORKDIR/pvc-shared.yaml" \
      || fail "slurm-shared-rwx PVC references wrong StorageClass"

    log "spot-check: nfs-provisioner Deployment uses NFS_SERVER + NFS_PATH from values"
    extract_resource "$k3s_render" Deployment nfs-subdir-external-provisioner \
      > "$WORKDIR/dep-nfs.yaml"
    grep -q 'value: "192.168.0.111"' "$WORKDIR/dep-nfs.yaml" \
      || fail "nfs-provisioner not parameterized with values-k3s.yaml NFS_SERVER"
    grep -q 'value: "/srv/nfs/k8s"' "$WORKDIR/dep-nfs.yaml" \
      || fail "nfs-provisioner not parameterized with values-k3s.yaml NFS_PATH"

    log "spot-check: storage.enabled=true without nfsServer must fail fast"
    if helm template slurm-platform "$CHART_DIR" --set storage.enabled=true -n "$NAMESPACE" \
        > "$WORKDIR/render-bad-storage.yaml" 2> "$WORKDIR/render-bad-storage.err"; then
      fail "expected helm template to fail when storage.enabled=true without nfsServer"
    fi
    grep -q 'requires storage.nfsServer to be set' "$WORKDIR/render-bad-storage.err" \
      || fail "storage guard message not raised on missing nfsServer"
  fi
fi

# Extract the content of a `<key>: |` block from a YAML stream, stripping the
# block's leading indent. Stops when the next sibling key (or shallower line)
# appears. Robust to inner content that itself contains lowercase `key:` lines.
extract_block() {
  local input="$1" key="$2"
  awk -v key="$key" '
    function indent_of(s,    m) { match(s, /^ */); return RLENGTH }
    in_block {
      if (length($0) == 0) { print ""; next }
      cur = indent_of($0)
      if (cur <= key_indent) { in_block = 0 }
      else { print substr($0, key_indent + 3); next }
    }
    !in_block {
      # match exact "<indent><key>: |" (allowing trailing whitespace)
      if (match($0, "^ *" key ": \\|[[:space:]]*$")) {
        key_indent = indent_of($0)
        in_block = 1
      }
    }
  ' "$input"
}

# --- 5. server-side dry-run (optional, requires reachable cluster) --------
if [[ -n "$SKIP_CLUSTER_DRYRUN" ]]; then
  log "server-side dry-run: SKIPPED (SKIP_CLUSTER_DRYRUN set)"
elif ! command -v kubectl >/dev/null; then
  warn "kubectl not found — skipping server-side dry-run"
elif ! kubectl version --request-timeout=3s >/dev/null 2>&1; then
  warn "kubectl cannot reach the API server — skipping server-side dry-run"
else
  # Some rendered resources target a namespace other than cluster.namespace
  # (e.g. gpu-operator's device-plugin-config ConfigMap). At install time
  # helm pre-install hooks create those namespaces; for dry-run we ensure
  # they exist (idempotent — re-running this script does not churn state).
  cross_ns="$(for r in "${RENDERS[@]}"; do grep -E '^  namespace: ' "$r"; done \
    | awk '{print $2}' | sort -u)"
  for ns in $cross_ns; do
    if ! kubectl get ns "$ns" >/dev/null 2>&1; then
      log "creating namespace $ns (so dry-run can validate references)"
      kubectl create namespace "$ns" >/dev/null
    fi
  done
  for label in "${!RENDERS[@]}"; do
    log "kubectl apply --dry-run=server ($label)"
    if ! kubectl apply --dry-run=server -f "${RENDERS[$label]}" \
        > "$WORKDIR/dryrun-$label.out" 2> "$WORKDIR/dryrun-$label.err"; then
      cat "$WORKDIR/dryrun-$label.err" >&2
      fail "kubectl dry-run failed for $label"
    fi
    count=$(grep -cE '(created|configured|unchanged) \(server dry run\)' "$WORKDIR/dryrun-$label.out" || true)
    log "  $count resources validated"
  done
fi

# --- 6. helm-unittest -----------------------------------------------------
if helm plugin list 2>/dev/null | grep -q '^unittest'; then
  if compgen -G "$CHART_DIR/tests/*_test.yaml" >/dev/null; then
    log "helm-unittest"
    helm unittest "$CHART_DIR" || fail "helm-unittest failed"
  else
    log "helm-unittest plugin installed but no tests/*_test.yaml — skipping"
  fi
else
  warn "helm-unittest plugin not installed — install with: helm plugin install https://github.com/helm-unittest/helm-unittest"
fi

log "ALL CHECKS PASSED"
