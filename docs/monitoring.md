# Phase 4：可觀測性實作規格

## 動機

本專案的核心主張是：Slurm 的批次排程語意 + Kubernetes 的彈性伸縮基礎設施，可以彌補彼此的不足。Phase 4 的目標是讓這個橋接過程**可以被觀測、被量測、被展示**。

```
Slurm 語意（queue / node states）
        ↓  Operator 橋接
K8s 行為（StatefulSet replicas / Pod lifecycle）
        ↓
Prometheus 收集 + Grafana 呈現
```

---

## 架構

```
┌─────────────────────────────────────────────────────────────────┐
│  namespace: slurm                                               │
│                                                                 │
│  slurm-exporter  ──→  /metrics（Slurm queue / node states）    │
│  slurm-elastic-operator  ──→  /metrics（scale events, guard）  │
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
│    └─ K8s Operator Dashboard                                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 元件說明

### 1. prometheus-slurm-exporter

**來源：** 自製（`docker/slurm-exporter/exporter.py`）

**部署方式：** 獨立 Deployment，部署於 `slurm` namespace，透過 Slurm REST API（slurmrestd）取得 job 與 node 狀態，使用 HS256 JWT 進行認證（key 來自 `slurm-jwt-secret`）。

**核心 metrics：**

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

**來源：** Kubernetes 官方 [`kubernetes/kube-state-metrics`](https://github.com/kubernetes/kube-state-metrics)

**部署方式：** 自維護 manifest（`manifests/monitoring/kube-state-metrics/kube-state-metrics.yaml`），含 ServiceAccount + ClusterRole + ClusterRoleBinding + Deployment + Service，部署於 `monitoring` namespace。

**使用的 metrics：**

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
```

---

## Dashboard 設計

### Bridge Overview（主 demo 看板）

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

---

## Demo 腳本

Phase 4 的標準 demo 流程，可在 Grafana Bridge Overview 看板上即時觀察：

```
1. 初始狀態
   → queue_pending = 0, replicas = 1（baseline worker）

2. 提交需要 2 個節點的 job
   sbatch -N 2 /shared/demo-job.sbatch
   → queue_pending = 1

3. Operator 偵測到 pending（下一個 poll cycle，~15s 內）
   → scale_up_total + 1
   → StatefulSet replicas: 1 → 2

4. 新 Pod ready，job 開始執行
   → queue_running = 1, queue_pending = 0
   → replicas_ready = 2

5. Job 完成
   → queue_running = 0
   → 等待 scale_down_cooldown（60s）

6. Operator 縮容
   → scale_down_total + 1
   → replicas: 2 → 1

7. 重複步驟 2，但這次讓 checkpoint 過舊
   → checkpoint_guard_blocks + 1
   → scale skipped（可在 Operator 看板觀察）
```

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

  - job_name: kube-state-metrics
    static_configs:
      - targets: ['kube-state-metrics.monitoring.svc.cluster.local:8080']

  - job_name: kubernetes-pods
    kubernetes_sd_configs:
      - role: pod
    relabel_configs:
      - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
        action: keep
        regex: "true"
```

---

## 部署步驟（已完成）

### bootstrap-monitoring.sh 執行內容

```
1. 確認 Phase 1–3 已部署（slurm namespace + slurm-controller + slurm-shared-rwx）
2. 建立 monitoring namespace
3. 部署 kube-state-metrics（manifest）
4. 建置 slurm-exporter image → kind load → 部署 Deployment + Service（REST API 模式）
5. 重建 operator image（加入 prometheus-client）→ kind load → rollout restart
6. 套用 prometheus ConfigMap + Deployment + Service
7. 套用 grafana Deployment + Service + dashboard ConfigMap
8. 套用跨 namespace NetworkPolicy（monitoring → slurm scrape）
9. 等待所有 Pod ready
10. 印出 port-forward 指令
```

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

---

## 與現有 Operator 的整合（已完成）

`operator/main.py` 已加入 `prometheus_client` 整合：

```python
from prometheus_client import Counter, Gauge, Histogram, start_http_server

# 在 __init__ 中初始化
scale_up_total = Counter('slurm_operator_scale_up_total', '...', ['pool'])
scale_down_total = Counter('slurm_operator_scale_down_total', '...', ['pool'])
checkpoint_guard_blocks = Counter('slurm_operator_checkpoint_guard_blocks_total', '...')
poll_duration = Histogram('slurm_operator_poll_duration_seconds', '...')
current_replicas = Gauge('slurm_operator_current_replicas', '...', ['pool'])

# 在 run() 中啟動 HTTP server
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

**驗證方式：**
```bash
kubectl -n slurm port-forward svc/slurm-elastic-operator 8000:8000
curl http://localhost:8000/metrics | grep slurm_operator
```

---

## 相關文件

- Phase 4 進度追蹤：README.md § Development Progress
- Operator 設計：`docs/note.md` § Operator 與部署流程改進
- prometheus-slurm-exporter：https://github.com/vpenso/prometheus-slurm-exporter
- kube-state-metrics：https://github.com/kubernetes/kube-state-metrics
- prometheus_client（Python）：https://github.com/prometheus/client_python
