# 🔭 Observability Spec

本專案的核心主張是：Slurm 的批次排程語意 + Kubernetes 的彈性伸縮基礎設施，可以彌補彼此的不足。Phase 4 的目標是讓這個橋接過程**可以被觀測、被量測、被展示**。因此實作監控面板，讓「Slurm 語意驅動 K8s 行為」這件事變得可視化，例如：

```
Slurm 語意（queue / node states）
        ↓  Operator 橋接
K8s 行為（StatefulSet replicas / Pod lifecycle）
        ↓
Prometheus 收集 + Grafana 呈現             ──→   scale skipped
```

---

## 架構

```
┌─────────────────────────────────────────────────────────────────┐
│  namespace: slurm                                               │
│                                                                 │
│  slurm-exporter  ──→  /metrics（Slurm queue / node states）    │
│  slurm-elastic-operator  ──→  /metrics（scale events, guard）  │
│  rl-scheduler  ──→  /metrics（DSAC decisions / live mode）     │
│  nvidia-dcgm-exporter（gpu-operator ns）──→ /metrics（GPU SM/VRAM）│
└────────────────────────────┬────────────────────────────────────┘
                             │ scrape
┌────────────────────────────▼────────────────────────────────────┐
│  namespace: monitoring                                          │
│                                                                 │
│  kube-state-metrics  ──→  Pod / StatefulSet states             │
│  Prometheus  ←──────────────────────────────────────────────── │
│       ↓                                                         │
│  Grafana                                                        │
│    ├─ Bridge Overview Dashboard（主 demo 看板）                  │
│    ├─ Slurm Cluster State Dashboard                            │
│    ├─ K8s Operator Dashboard                                   │
│    ├─ GPU Utilisation (DCGM) Dashboard                         │
│    └─ Scheduler Live Resource View（DSAC / queue / MPS）        │
└─────────────────────────────────────────────────────────────────┘
```

---

## 元件說明

### 1. prometheus-slurm-exporter

- 來源：自製（`docker/slurm-exporter/exporter.py`）
- 部署方式：獨立 Deployment，部署於 `slurm` namespace，透過 Slurm REST API（slurmrestd）取得 job 與 node 狀態，使用 HS256 JWT 進行認證（key 來自 `slurm-jwt-secret`）。
- 核心 metrics：

| Metric | 類型 | 說明 |
|--------|------|------|
| `slurm_queue_pending` | Gauge | 目前 PENDING 狀態的 job 數 |
| `slurm_queue_running` | Gauge | 目前 RUNNING 狀態的 job 數 |
| `slurm_nodes_idle` | Gauge | 閒置節點數 |
| `slurm_nodes_alloc` | Gauge | 已分配（allocated/mixed）節點數 |
| `slurm_nodes_down` | Gauge | DOWN/DRAIN/NOT_RESPONDING 狀態節點數 |
| `slurm_nodes_draining` | Gauge | DRAIN overlay 節點數（不接受新 job） |
| `slurm_nodes_total` | Gauge | Slurm 登錄節點總數 |
| `slurm_job_queue_oldest_wait_seconds` | Gauge | 最久 pending job 的等待秒數 |
| `slurm_job_queue_avg_wait_seconds` | Gauge | 所有 pending jobs 的平均等待秒數 |
| `slurm_scheduler_cycle_last_seconds` | Gauge | 最近一次排程 cycle 耗時（秒） |
| `slurm_scheduler_cycle_mean_seconds` | Gauge | 排程 cycle 平均耗時（秒） |
| `slurm_backfill_cycle_last_seconds` | Gauge | 最近一次 backfill cycle 耗時（秒） |
| `slurm_backfill_queue_length` | Gauge | backfill 排程器考慮的 job 數 |
| `slurm_exporter_scrape_success` | Gauge | 最近一次抓取是否成功（1/0） |

### 2. kube-state-metrics

- 來源：Kubernetes 官方 [`kubernetes/kube-state-metrics`](https://github.com/kubernetes/kube-state-metrics)
- 部署方式：自行維護 manifest（`manifests/monitoring/kube-state-metrics/kube-state-metrics.yaml`），含 ServiceAccount + ClusterRole + ClusterRoleBinding + Deployment + Service，部署於 `monitoring` namespace。
- 使用的 metrics：

| Metric | 說明 |
|--------|------|
| `kube_statefulset_replicas` | StatefulSet 目標 replica 數 |
| `kube_statefulset_status_replicas_ready` | 已 Ready 的 replica 數 |
| `kube_pod_status_phase` | Pod 各 phase 計數 |
| `kube_pod_status_ready` | Pod ready condition |

### 3. Operator 自定義 Metrics

在 `operator/main.py` 中加入 `prometheus_client` HTTP server，暴露以下 metrics：

| Metric | 類型 | 說明 |
|--------|------|------|
| `slurm_operator_scale_up_total` | Counter | 觸發 scale-up 的次數，label: `pool` |
| `slurm_operator_scale_down_total` | Counter | 觸發 scale-down 的次數，label: `pool` |
| `slurm_operator_scale_skipped_total` | Counter | scale 被跳過的次數，label: `pool`, `reason` |
| `slurm_operator_checkpoint_guard_blocks_total` | Counter | Checkpoint Guard 攔截 scale-down 的次數 |
| `slurm_operator_poll_duration_seconds` | Histogram | 每次 poll loop 耗時 |
| `slurm_operator_current_replicas` | Gauge | 各 pool 目前 replica 數，label: `pool` |

**Port：** `8000`（`/metrics` endpoint）

### 4. NVIDIA DCGM exporter

- 來源：NVIDIA GPU Operator 內建的 `nvidia-dcgm-exporter`

- 部署方式：`scripts/install-gpu-operator.sh` 安裝 GPU Operator 時開啟：

```bash
--set dcgmExporter.enabled=true
--set dcgmExporter.serviceMonitor.enabled=false
```

ServiceMonitor 維持關閉，因為本專案使用 chart 內建 Prometheus；Prometheus 透過
`chart/templates/monitoring/prometheus.yaml` 的 static scrape job 抓：
`nvidia-dcgm-exporter.gpu-operator.svc.cluster.local:9400`。

Chart 開關：

```yaml
monitoring:
  dcgmExporter:
    enabled: true
    namespace: gpu-operator
    serviceName: nvidia-dcgm-exporter
    port: 9400
```

核心 metrics：

| Metric | 類型 | 說明 |
|--------|------|------|
| `DCGM_FI_DEV_GPU_UTIL` | Gauge | 真實 GPU SM 使用率，不是 Slurm allocated GPU count |
| `DCGM_FI_DEV_FB_USED` | Gauge | VRAM 已用量 |
| `DCGM_FI_DEV_FB_FREE` | Gauge | VRAM 可用量 |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | Gauge | Memory controller active ratio，可抓 bandwidth-bound workload |
| `DCGM_FI_DEV_GPU_TEMP` | Gauge | GPU 溫度 |
| `DCGM_FI_DEV_POWER_USAGE` | Gauge | GPU 板卡功耗 |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | Gauge | Tensor Core busy ratio；硬體/driver 支援時可用 |

DCGM 指標補上了 Slurm-only monitoring 的盲點：Slurm 知道「GPU 被分配」，
但不知道該 GPU 是否真的在算、VRAM 是否接近 OOM、或是否只是 idle allocation。

### 5. RL Scheduler / DSAC Metrics

- 來源：自製（`services/rl_scheduler/serve.py`）
- 部署方式：`rl-scheduler` Deployment，部署於 `slurm` namespace，透過 `/snapshot` 接收 cluster snapshot，透過 `/decide` 讓 Slurm `job_submit.lua` 取得 DSAC priority boost。
- 監控用途：確認 RL 是否真的處於 live mode、是否有對 job 做出 boost、snapshot 是否過期，以及最近一次 DSAC 選到的 job / node / GPU。

**Port：** `8002`（`/metrics` endpoint）

核心 metrics：

| Metric | 類型 | 說明 |
|--------|------|------|
| `rl_scheduler_ready` | Gauge | DSAC model 是否已載入，1 表示 ready |
| `rl_scheduler_shadow_mode` | Gauge | 1 表示 shadow mode，0 表示 live mode |
| `rl_scheduler_decisions_total` | Counter | DSAC 決策次數，label: `result=selected/no_boost/abstain` |
| `rl_scheduler_priority_boost_total` | Counter | 回傳正 priority boost 的累積次數 |
| `rl_scheduler_last_priority_boost` | Gauge | 最近一次 `/decide` 回傳的 priority boost |
| `rl_scheduler_policy_value` | Gauge | 最近一次 DSAC decision 的 value estimate |
| `rl_scheduler_policy_entropy` | Gauge | 最近一次 DSAC policy entropy |
| `rl_scheduler_snapshot_age_seconds` | Gauge | 目前 cached snapshot 的年齡秒數 |
| `rl_scheduler_snapshot_pending_jobs` | Gauge | snapshot 中 pending jobs 數 |
| `rl_scheduler_snapshot_free_mps` | Gauge | snapshot 中可用 MPS slot 總量 |
| `rl_scheduler_last_action` | Gauge | 最近一次 DSAC 選到的 flat action index |
| `rl_scheduler_last_job_index` | Gauge | 最近一次 DSAC 選到的 job slot，no-op / abstain 為 -1 |
| `rl_scheduler_last_node_index` | Gauge | 最近一次 DSAC 選到的 node index，no-op / abstain 為 -1 |
| `rl_scheduler_last_gpu_index` | Gauge | 最近一次 DSAC 選到的 GPU index，no-op / abstain 為 -1 |

Prometheus 從 `monitoring` namespace scrape `rl-scheduler.slurm.svc.cluster.local:8002`。
因為 `slurm` namespace 有 default-deny NetworkPolicy，chart 也會建立
`allow-prometheus-scrape-rl-scheduler`，放行 Prometheus 到 `rl-scheduler:8002` 的 ingress。

---

## 檔案結構

```
phase4/
├── docker/
│   └── slurm-exporter/
│       ├── Dockerfile
│       └── exporter.py                    # 自製 exporter（Slurm REST API + JWT）
├── manifests/
│   ├── monitoring-namespace.yaml          # namespace: monitoring
│   ├── network-policy-monitoring.yaml     # monitoring → slurm 跨 namespace scrape
│   ├── alertmanager/
│   │   └── alertmanager.yaml              # Deployment + ConfigMap + Service
│   ├── prometheus/
│   │   ├── alert-rules-cm.yaml            # PrometheusRule / alerting rules
│   │   ├── prometheus-config.yaml         # ConfigMap: scrape configs（含 RBAC）
│   │   └── prometheus-deployment.yaml     # Deployment + Service
│   ├── grafana/
│   │   ├── grafana-deployment.yaml        # Deployment + Service
│   │   ├── grafana-dashboards-cm.yaml     # ConfigMap: dashboard JSON
│   │   └── grafana-provisioning-cm.yaml   # ConfigMap: datasource + dashboard 掛載設定
│   ├── kube-state-metrics/
│   │   └── kube-state-metrics.yaml        # Deployment + Service + RBAC
│   └── slurm-exporter/
│       └── slurm-exporter-deployment.yaml # Deployment + Service（REST API 模式，無需 exec RBAC）
└── scripts/
    ├── bootstrap-monitoring.sh            # 一鍵部署監控堆疊
    └── verify-monitoring.sh               # 驗證 metrics 可正常抓取
chart/
└── dashboards/
    ├── bridge-overview.json               # Slurm↔K8s Bridge Overview
    ├── gpu.json                           # GPU Utilisation (DCGM) — cluster-wide
    ├── operator.json                      # K8s Elastic Operator
    ├── per-job-gpu.json                   # R20 (v5) Per-Job GPU Profile (DCGM + OTel)
    ├── scheduler-live.json                # Scheduler Live Resource View (DSAC / queue / MPS)
    └── sla-efficiency.json                # SLA / efficiency
```

---

## Dashboard 設計

### Bridge Overview

這是最重要的一塊看板，視覺化呈現「Slurm 語意驅動 K8s 行為」。

**Row 1：當下狀態（Stat panels）**
- Pending Jobs（`slurm_queue_pending`）
- Running Jobs（`slurm_queue_running`）
- Worker Replicas Ready（`kube_statefulset_status_replicas_ready{statefulset="slurm-worker-cpu"}`）
- Scale Events Today（`increase(slurm_operator_scale_up_total[24h])`）

**Row 2：橋接時序（Time series，共一張圖）**
- Y 左軸：Slurm queue depth（pending jobs）
- Y 右軸：K8s StatefulSet replicas
- Annotations：scale-up / scale-down events
- 視覺效果：pending 升高 → replicas 跟著增加，queue 清空 → replicas 回落

**Row 3：延遲分析**
- Provisioning Latency：從 scale-up 事件到 `kube_statefulset_status_replicas_ready` 增加的時間差
- Job Wait Time distribution（來自 sacct，若 slurm-exporter 支援）

### Slurm Cluster State Dashboard

- Node States 圓餅圖（IDLE / ALLOC / DOWN / DRAIN）
- 各 partition queue depth 時序
- Job 完成率（running → completed per hour）

### K8s Operator Dashboard

- Poll loop duration histogram（`slurm_operator_poll_duration_seconds`）
- Scale event timeline（scale-up / scale-down / skipped / guard-blocked，以 annotations 標記）
- Checkpoint Guard 攔截事件計數
- 各 pool 的 replica count 時序（cpu / gpu-a10 / gpu-h100）

### GPU Utilisation (DCGM) Dashboard

`chart/dashboards/gpu.json` 提供真實 GPU 使用狀態，dashboard uid 為
`slurm-k8s-gpu`，title 為 `GPU Utilisation (DCGM)`。

**Panels：**

- GPU SM Utilisation (%)：`DCGM_FI_DEV_GPU_UTIL`
- VRAM Used / Free (MiB)：`DCGM_FI_DEV_FB_USED`、`DCGM_FI_DEV_FB_FREE`
- Memory Copy Util (%)：`DCGM_FI_DEV_MEM_COPY_UTIL`
- GPU Temperature (°C)：`DCGM_FI_DEV_GPU_TEMP`
- GPU Power Draw (W)：`DCGM_FI_DEV_POWER_USAGE`
- GPU Util (Now)：目前各 GPU 的 SM% stat panel
- VRAM Used (Now)：目前各 GPU 的 VRAM 使用量 stat panel

> 這個 dashboard 取代早期用 Slurm allocation count 推估 GPU utilization 的做法。Slurm allocation count 只能表示「排程器分配了 GPU」，DCGM 才能回答「硬體實際忙不忙」。

### Per-Job GPU Profile (R20)

`chart/dashboards/per-job-gpu.json`，dashboard uid `slurm-k8s-per-job-gpu`，是 v5 review 補上的「per-job × per-GPU × full-lifecycle」差異化面板。

**設計取捨：** dcgm-exporter 標籤上沒有 `slurm_job_id`（NVIDIA 上游沒做這層 join），所以 dashboard 用兩個 template variable 串起來：

- `$hostname` — 從 `DCGM_FI_DEV_GPU_UTIL` 的 `Hostname` label 拉出選單；使用者用 `squeue -j <jobid> -o '%N'` 查到 worker 後手選
- `$gpu` — 同一 host 上的 GPU index；預設 `All`
- `$job_id` — textbox，用來組裝 Tempo TraceQL 連結（`job_id=$job_id`），需 Phase 7-A OTel 啟用

**Panels：**

1. **GPU profile（4 個 timeseries）**：SM%、VRAM、Memory Copy Util、Power、Temperature — 全部 filter by `$hostname / $gpu`
2. **Pool provisioning latency p95**：`slurm_operator_provisioning_latency_seconds_bucket`，**exemplar 啟用** → 任一資料點上的 exemplar 點按下去會跳到 Tempo 的 OTel trace
3. **Open Tempo trace 連結**：deep-link 到 Grafana Explore，TraceQL 預填 `job_id=$job_id`

這個 dashboard 對應 Slinky/SUNK/ParallelCluster 都沒有的「single-job lifecycle drill-down」差異化功能，請見 [`docs/review.md §4.2`](review.md)。

### Scheduler Live Resource View

`chart/dashboards/scheduler-live.json`，dashboard uid `slurm-scheduler-live`，title 為
`Scheduler Live Resource View`。這個 dashboard 是給一般使用者看的 live 資源流向面板，第一排用非圖表的流程圖呈現：

```text
Submit → DSAC → Priority → Workers
```

**Panels：**

- Resource Flow：用圖示式流程區塊說明 job 從 `sbatch`、DSAC 決策、priority boost 到 worker 執行的路徑
- Pending / Running / Oldest Wait：`slurm_queue_pending`、`slurm_queue_running`、`slurm_job_queue_oldest_wait_seconds`
- DSAC Live Mode：`1 - rl_scheduler_shadow_mode`，確認目前不是 shadow mode
- Last Priority Boost：`rl_scheduler_last_priority_boost`
- Snapshot Age：`rl_scheduler_snapshot_age_seconds`
- DSAC Decisions：`increase(rl_scheduler_decisions_total{result=...}[5m])`
- Last DSAC Action：`rl_scheduler_last_job_index`、`rl_scheduler_last_node_index`、`rl_scheduler_last_gpu_index`
- DSAC Confidence Signals：`rl_scheduler_policy_value`、`rl_scheduler_policy_entropy`
- Free MPS Slots in Snapshot：`rl_scheduler_snapshot_free_mps`
- GPU Utilization and VRAM：`DCGM_FI_DEV_GPU_UTIL`、`DCGM_FI_DEV_FB_USED / DCGM_FI_DEV_FB_TOTAL`
- Worker Replicas and Ready Ratio：`slurm_operator_current_replicas`、`slurm_operator_pods_ready`

---

## Prometheus Scrape Config

```yaml
# manifests/monitoring/prometheus/prometheus-config.yaml（摘錄）
scrape_configs:
  - job_name: slurm-exporter
    static_configs:
      - targets: ['slurm-exporter.slurm.svc.cluster.local:9341']

  - job_name: slurm-operator
    static_configs:
      - targets: ['slurm-elastic-operator.slurm.svc.cluster.local:8000']

  - job_name: rl-scheduler
    static_configs:
      - targets: ['rl-scheduler.slurm.svc.cluster.local:8002']

  - job_name: kube-state-metrics
    static_configs:
      - targets: ['kube-state-metrics.monitoring.svc.cluster.local:8080']

  - job_name: dcgm-exporter
    static_configs:
      - targets: ['nvidia-dcgm-exporter.gpu-operator.svc.cluster.local:9400']

  - job_name: kubernetes-pods
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
```

---

## 部署步驟 ✅️

```bash
bash scripts/bootstrap-monitoring.sh   # 一鍵部署
bash scripts/verify-monitoring.sh      # 驗證（所有 metrics endpoint 可抓）
```

### 存取方式

```bash
# Grafana
kubectl -n monitoring port-forward svc/grafana 3000:3000
# http://localhost:3000  (預設帳密 admin/admin)

# Prometheus（debug 用）
kubectl -n monitoring port-forward svc/prometheus 9090:9090
# http://localhost:9090
```

### DCGM 驗證

```bash
# 確認 GPU Operator 有部署 DCGM exporter
kubectl -n gpu-operator get svc nvidia-dcgm-exporter
kubectl -n gpu-operator get pods -l app=nvidia-dcgm-exporter

# 直接看 raw metrics
kubectl -n gpu-operator port-forward svc/nvidia-dcgm-exporter 9400:9400
curl -s http://localhost:9400/metrics | grep -E 'DCGM_FI_DEV_GPU_UTIL|DCGM_FI_DEV_FB_USED|DCGM_FI_DEV_POWER_USAGE' | head

# Prometheus 查詢
kubectl -n monitoring port-forward svc/prometheus 9090:9090
# 在 Prometheus UI 查 DCGM_FI_DEV_GPU_UTIL / DCGM_FI_DEV_FB_USED
```

### RL Scheduler / DSAC 驗證

```bash
# 直接看 raw metrics
kubectl -n slurm port-forward svc/rl-scheduler 8002:8002
curl -s http://localhost:8002/metrics | grep rl_scheduler

# Prometheus 查詢
kubectl -n monitoring port-forward svc/prometheus 9090:9090
# 在 Prometheus UI 查：
#   rl_scheduler_ready
#   rl_scheduler_shadow_mode
#   rl_scheduler_decisions_total
#   rl_scheduler_last_priority_boost
#   rl_scheduler_snapshot_free_mps
```

---

## 與現有 Operator 的整合 ✅️

`operator/main.py` 已加入 `prometheus_client` 整合：

```python
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# 在 __init__ 中初始化
scale_up_total = Counter('slurm_operator_scale_up_total', '...', ['pool'])
scale_down_total = Counter('slurm_operator_scale_down_total', '...', ['pool'])
checkpoint_guard_blocks = Counter('slurm_operator_checkpoint_guard_blocks_total', '...')
poll_duration = Histogram('slurm_operator_poll_duration_seconds', '...')
current_replicas = Gauge('slurm_operator_current_replicas', '...', ['pool'])

start_http_server(8000)
```

`manifests/operator/slurm-elastic-operator.yaml` 已包含：

```yaml
ports:
  - name: metrics
    containerPort: 8000
    protocol: TCP
```

並已加入對應 Service（`slurm-elastic-operator` port 8000），讓 Prometheus 可以 scrape。

驗證方式：
```bash
kubectl -n slurm port-forward svc/slurm-elastic-operator 8000:8000
curl http://localhost:8000/metrics | grep slurm_operator
```

---

## 相關文件

- prometheus-slurm-exporter：https://github.com/vpenso/prometheus-slurm-exporter
- kube-state-metrics：https://github.com/kubernetes/kube-state-metrics
- prometheus_client（Python）：https://github.com/prometheus/client_python
