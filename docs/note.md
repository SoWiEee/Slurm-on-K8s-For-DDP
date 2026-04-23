# Development Notes

這份筆記保留原本的階段紀錄，以及這一輪開發實際踩到的坑。

---

# Phase 1

- 建立 Slurm Controller / Worker 映像。
- 在 Kind 部署靜態 Slurm 叢集。
- 讓 Pod 間具備 SSH 互通與 Munge 認證。

## Debug Record

### 問題 1：Secret volume 唯讀，不能直接 chmod

觀察到：
```
chmod: changing permissions of '/etc/munge/munge.key': Read-only file system
```
原因在於 K8s Secret mount 是唯讀。

修正方式：

- 改掛到 `/slurm-secrets/munge.key`。
- 啟動時複製到 `/etc/munge/munge.key` 後再 `chown/chmod`。

### 問題 2：`SlurmctldHost` / DNS 解析錯誤

觀察到：
```
This host ... not a valid controller
NO NETWORK ADDRESS FOUND
```

修正方式：

- 在 `slurm.conf` 明確設定 `NodeAddr` / `NodeHostname`。
- controller 改用 `slurm-controller-0(slurm-controller-0....svc.cluster.local)`。

---

# Phase 2

- 開發 Python Operator，實作 `Pending Job -> Scale Up`。
- 實作 `Idle Node -> Scale Down`。
- 讓整個 Phase 1 + Phase 2 能以 `bootstrap.sh` / `verify.sh` 穩定驗證。
- (2-B) 加入結構化日誌，讓 autoscaling 行為可追蹤、可分析、可做報告。
- (2-C) 把單一 worker pool 擴展成 multi-pool / partition-aware autoscaling。
- (2-D) 加入 checkpoint-aware scale-down guard，避免正在跑的工作因過早縮容而丟失恢復點。
- (2-E) 在單一叢集引入兩個子網路，分成 management subnet 和 data subnet。
  - Slurm 控制流量維持單純
  - 之後若要做 PyTorch DDP / MPI / NCCL，能逐步把高流量傳輸導向 `data subnet`
  - verify 時也能清楚展示哪些元件是 control plane、哪些元件是 dual-homed compute plane

## Debug Record

### 問題 1：`duplicate partition in config: debug`

觀察到 operator 啟動時直接 `ValueError: duplicate partition in config: debug`

原因是：原本 validation 把「partition 名稱重複」當成非法，但現在多 pool 共享同一個 Slurm partition 是設計需求，不是錯誤。

修正方法：

- validation 不能用 partition name 當唯一鍵。
- 要接受「同一 partition 對應多個 worker pool」。

# Phase 3

- 在 Kind 單機環境部署 NFS Server 並整合 `nfs-subdir-external-provisioner`。
- 建立 StorageClass + RWX PVC。
- 將 Controller / Worker / Login 掛載共享儲存。
- 將 Phase 2-D 的 checkpoint-aware guard 與真實 workload 串起來。

## Debug Record

確認 Phase 1/2 完成後，使用者是否可以從 Login Pod 提交 `sbatch` 並取回輸出檔案（`*.out`、`*.err`）？

### Phase 1/2 的限制分析

`slurm-login.yaml`（Phase 1/2 版本）的 volumeMounts 只有：

```
/etc/slurm           ← ConfigMap（唯讀）
/slurm-secrets       ← Secret（唯讀）
/opt/slurm-runtime-src ← ConfigMap（唯讀）
```

Worker pods 同樣**沒有任何共享可寫 Volume**。

Slurm 預設將 `slurm-<JOBID>.out` 寫到 `$SLURM_SUBMIT_DIR`，但 job 是在 worker pod 上執行的，worker 的本機 ephemeral 檔案系統與 login pod 完全隔離。因此：

- `srun`（互動式）：stdout 透過 Slurm I/O forwarding 回傳，**可用**。
- `sbatch`：job 成功提交與執行，但輸出檔案寫在 worker 的本機磁碟，login pod **看不到**。

verify.sh 的 smoke test 刻意使用 `sleep N` job，迴避了這個問題。

### Phase 3 實作現況

Phase 3 **已完成實作**，並非只有設計：

| 檔案 | 內容 |
|------|------|
| `scripts/setup-nfs-server.sh` | 在 Windows 11 主機上建立 NFS Server（`/srv/nfs/k8s`） |
| `manifests/storage/nfs-subdir-provisioner.tmpl.yaml` | NFS subdir external provisioner Deployment（需替換 `__NFS_SERVER__` / `__NFS_PATH__`） |
| `manifests/storage/shared-storage.yaml` | StorageClass `slurm-shared-nfs` + PVC `slurm-shared-rwx`（20Gi RWX） |
| `scripts/bootstrap-storage.sh` | 部署 provisioner → 建立 PVC → patch controller/worker/login 加入 `/shared` mount |
| `scripts/verify-storage.sh` | 驗證 PVC Bound + `/shared` 掛載在所有 pod 上 |
| `scripts/verify-storage-e2e.sh` | **完整 e2e 測試**：operator scale-up → login 提交多節點 `sbatch` → 等待 job COMPLETING → 從 login 讀回 `/shared/` 輸出驗證 |

Phase 3 部署後，`/shared` 以 ReadWriteMany 方式同時掛載到：
- `slurm-controller-0`
- `slurm-worker-cpu-*`（以及所有副本）
- `slurm-login`

使用者只需在 job script 加入：

```bash
#SBATCH --output=/shared/out-%j.txt
#SBATCH --error=/shared/err-%j.txt
```

job 完成後即可在 login pod 的 `/shared/` 直接讀取輸出（因共享 NFS，任何 pod 均可讀）。

**E2E 測試說明：** `verify-storage-e2e.sh` 驗證的是多節點場景下的完整生命週期：
1. 暫停 operator → 提交帶 `--hold` 的觸發 job（確保 cpu-1 不立即被搶）
2. 恢復 operator → scale-up 到 2 台 worker
3. 等待 cpu-1 變 `idle`（含 fix_node_addr 修正 IP cache + SIGHUP fallback）
4. 取消 trigger job → 提交真實 2 節點 smoke job（operator 繼續暫停，避免縮容干擾）
5. 等待 job 完成，從 login pod 讀取 `/shared/` 輸出驗證每台 worker 的 hostname

### 部署順序建議

在 Phase 2 + Phase 3 同時啟用時，建議部署順序為：

```
1. bash scripts/bootstrap.sh              # Phase 1 + Phase 2
2. sudo bash scripts/setup-nfs-server.sh  # 主機端 NFS（一次性，在 WSL2 執行）
3. NFS_SERVER=<wsl2-ip> bash scripts/bootstrap-storage.sh
4. bash scripts/verify-storage.sh
5. bash scripts/verify-storage-e2e.sh    # WORKER_STS 預設 slurm-worker-cpu
```

---

# Phase 4 (DDP + 可觀測性)

Phase 4 已完成可觀測性部分（Prometheus + Grafana，詳見下方第 4 節）。DDP 工作負載部分為設計規格與 TODO，待未來整合。

## 1. Worker Image 加入 PyTorch（DDP 前置條件）

目前 worker image 只有 Slurm + Munge + OpenMPI（Phase 5 已加入），要跑 DDP 還需要加入 PyTorch：

```dockerfile
# docker/worker/Dockerfile 加入
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
# Kind 環境無真實 GPU，先用 CPU 版本驗證 DDP 控制流
```

---

## 2. 標準 DDP sbatch 腳本

```bash
#!/usr/bin/env bash
#SBATCH -J ddp-train
#SBATCH -p debug
#SBATCH -N 2
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH -o /shared/ddp-out-%j.txt
#SBATCH -e /shared/ddp-err-%j.txt

# 從 Slurm 提供的 SLURM_NODELIST 取第一個節點當 rendezvous master
MASTER_ADDR=$(scontrol show hostnames "$SLURM_NODELIST" | head -n 1)
MASTER_PORT=29500
export MASTER_ADDR MASTER_PORT

srun torchrun \
  --nnodes=$SLURM_NNODES \
  --nproc_per_node=1 \
  --rdzv_backend=c10d \
  --rdzv_endpoint=${MASTER_ADDR}:${MASTER_PORT} \
  /shared/train.py \
  --checkpoint-dir /shared/checkpoints/job-${SLURM_JOB_ID}
```

**參考來源：** AWS ParallelCluster sbatch 範本（`github.com/aws/aws-parallelcluster`）

---

## 3. SIGTERM → Checkpoint 儲存串接 Checkpoint Guard

**現狀：** checkpoint guard 只檢查 `/shared/checkpoints/latest.pt` 的 mtime，但訓練腳本沒有處理 SIGTERM，scale-down 前節點被直接回收，checkpoint 不一定有被寫入。

**目標改法：** 訓練腳本加 SIGTERM handler：

```python
import signal, torch, torch.distributed as dist

def save_and_exit(sig, frame):
    if dist.get_rank() == 0:
        torch.save({"epoch": epoch, "model": model.state_dict()},
                   "/shared/checkpoints/latest.pt")
    dist.barrier()
    exit(0)

signal.signal(signal.SIGTERM, save_and_exit)
```

**完整串接流程：**
```
Operator 決定 scale-down
  → kubectl drain（送 SIGTERM 給 slurmd）
  → slurmd 轉發給訓練 process
  → SIGTERM handler 寫 checkpoint
  → checkpoint guard 確認 mtime 夠新
  → scale-down 執行
```

**參考來源：** PyTorch Elastic torchelastic (`github.com/pytorch/elastic`) 的 signal handling 模式

---

## 4. Prometheus + Grafana 監控（已完成，Phase 4）

詳細規格見 `docs/monitoring.md`。核心是三層 metrics：

| 來源 | 取得方式 | 關鍵指標 |
|------|---------|---------|
| slurm-exporter | scrape slurmrestd（REST API） | queue_pending, nodes_idle, nodes_alloc |
| kube-state-metrics | K8s 原生 | StatefulSet replicas, Pod ready |
| operator 自定義 | prometheus_client HTTP server（port 8000） | scale_up/down_total, guard_blocks, poll_duration |

Phase 4 已部署 Prometheus + Grafana + slurm-exporter + kube-state-metrics。Grafana 提供三個看板：
- **Bridge Overview**：視覺化 Slurm queue depth 與 K8s StatefulSet replicas 的聯動關係
- **Slurm Cluster State**：node states 圓餅圖、各 partition queue depth 時序
- **K8s Operator**：scale event timeline、poll duration histogram、guard block 計數

**部署：** `bash scripts/bootstrap-monitoring.sh`  
**驗證：** `bash scripts/verify-monitoring.sh`

**參考來源：** `github.com/SlinkyProject/slurm-exporter`（REST API 驅動）、`github.com/vpenso/prometheus-slurm-exporter`（exec 驅動，較易入門）

---

## 建議加入的改進（可選）

### 5. Job 提交前健康檢查（pre-check）

**參考來源：** Character.ai Slonk（`blog.character.ai/slonk/`）

在 sbatch 腳本開頭加一個 pre-check stage，確認每個分配到的節點 `/shared` 掛載正常後再開始 torchrun：

```bash
# sbatch 前置 pre-check
srun bash -c "test -d /shared && echo ok || echo fail" | grep -v ok && exit 1
```

收益：降低 DDP job 因 NFS 掛載異常或節點網路問題在訓練中途失敗的機率。

---

### 6. Elastic DDP（動態 world size）

**參考來源：** PyTorch Elastic `torchrun --nnodes=min:max`

允許 operator scale-up 後，新節點可以在訓練過程中動態加入（而非只在 job 開始時固定 world size）：

```bash
torchrun \
  --nnodes=1:4 \          # 允許 1 到 4 個節點
  --nproc_per_node=1 \
  --rdzv_backend=c10d ...
```

這讓 operator 的 scale-up 決策能更即時反映到訓練效率。

---

### 7. Soperator reconciliation loop 設計參考

**參考來源：** `github.com/nebius/soperator/internal/controller/`

Soperator 在 scale-down 前會確認沒有 RUNNING 狀態的 job 在該節點，再做 StatefulSet 變更。可以參考其 reconciliation loop 設計，加強目前 operator 的 scale-down 安全性，避免 operator 更新或 manifest re-apply 時意外殺掉正在訓練的 job。

---

## 相關開源專案對照

| 改進項目 | 主要參考來源 | 備註 |
|---------|------------|------|
| REST API | SlinkyProject/slurm-exporter | SchedMD 官方，生產驗證 |
| SIGTERM handler | pytorch/elastic | torchelastic 官方模式 |
| Pre-check | Character.ai Slonk | blog.character.ai/slonk |
| Scale-down 安全性 | nebius/soperator | Go+Kubebuilder，架構參考 |
| sbatch + torchrun 整合 | aws/aws-parallelcluster | 業界驗證的環境變數設定 |
| Prometheus monitoring | vpenso/prometheus-slurm-exporter | exec 版本，入門用 |
| Prometheus monitoring | SlinkyProject/slurm-exporter | REST API 版本，演進目標 |

## Debug Record

### A. slurm-exporter 無法連線到 slurmrestd（NetworkPolicy 缺規則）

**症狀：**
```
urllib.error.URLError: <urlopen error timed out>
```
slurm-exporter 每次 scrape 都超時，所有 Slurm 指標歸零（`slurm_queue_pending=0`、`slurm_nodes_total=0`），但 `/metrics` endpoint 本身可正常被 Prometheus 抓到（`slurm_exporter_scrape_success=0`）。

**根因：**

`manifests/networking/network-policy.yaml` 的 `allow-controller-ingress` policy 允許的 source pod 清單為：
```yaml
values:
  - slurm-worker-cpu
  - slurm-worker-gpu-a10
  - slurm-worker-gpu-h100
  - slurm-login
  - slurm-elastic-operator
```
缺少 `slurm-exporter`。預設 deny-all ingress policy 因此擋掉了 exporter → slurmrestd（port 6820）的連線。

**修正：**

在 `allow-controller-ingress` 的 values 清單加入 `slurm-exporter`，再執行：
```bash
kubectl apply -f manifests/networking/network-policy.yaml
kubectl -n slurm rollout restart deployment/slurm-exporter
```

---

### B. verify-monitoring.sh 在無 wget/curl 的 image 裡 exec 失敗

**症狀：**

verify script 對 kube-state-metrics 和 slurm-exporter 的 metrics 檢查全部 FAIL，但 Prometheus 上這兩個 target 都是 UP。

**根因（三個獨立問題）：**

1. **distroless image 無 shell/wget/curl**：`check_metrics_endpoint` 用 `kubectl exec pod -- wget ...` 的方式，但 kube-state-metrics 使用的 image 沒有這些工具，exec 直接失敗。

2. **python:3.11-slim 無 wget/curl**：slurm-exporter image 同樣無 curl/wget。

3. **Windows 環境無 `python3` 指令**：Prometheus target 解析原本用 `python3 -c "..."` 做 JSON 解析，Windows 下指令應為 `py`，導致腳本報錯誤而非解析失敗，造成誤判。

**修正：**

改用 **port-forward + host-side curl** 的方式取代 exec：
- 每個 metrics 檢查改為：啟動 `kubectl port-forward`，等待 3 秒，用 host 上的 `curl` 抓 `/metrics`，直接 pipe 給 `grep`，完成後 kill port-forward。
- Prometheus targets 解析從 `python3` JSON 解析改為 `grep -A5 ... | grep '"health":"up"'`，無需任何 Python。
- 同時修正 kube-state-metrics 的 metric 名稱：實際名稱為 `kube_statefulset_status_replicas`（非 `kube_statefulset_replicas`）。

---

# Phase 5 技術規劃：平台化與高可用

Phase 5 的核心問題是：**如何讓這套系統從研究原型演進成能交付給其他人使用的平台？**

目標受眾（TA）有兩類：
1. **AI 研究平台工程師**（學術單位、企業內部 MLOps）：需要一鍵部署、多租戶隔離、SLO 告警整合到既有 on-call 流程。
2. **雲端架構師 / SRE**：需要 HA、可觀測性（traces, not just metrics）、以及能和 Terraform / ArgoCD 整合的 IaC 交付形式。

---

## 5-A：Helm Chart 封裝

### 問題
目前部署流程是「依序執行多支 bootstrap 腳本，每支腳本依賴前一支的副作用」。這讓：
- 環境差異（本機 Kind vs. 雲端 EKS）需要修改腳本而非修改參數。
- 版本升級沒有 rollback 機制。
- 無法用 ArgoCD / Flux 做 GitOps 管理。

### 設計
```
chart/
  Chart.yaml
  values.yaml              ← 所有可調參數的預設值
  values-dev.yaml          ← Kind 本機覆蓋
  values-prod.yaml         ← EKS / GKE 雲端覆蓋
  templates/
    phase1/                ← controller + worker StatefulSets
    phase2/                ← operator + RBAC
    phase3/                ← NFS provisioner（可選）
    phase4/                ← Prometheus + Grafana + Alertmanager（可選）
    _helpers.tpl           ← 共用 label / name 函數
```

關鍵 `values.yaml` 參數：
```yaml
cluster:
  name: slurm-lab
  namespace: slurm

pools:
  cpu:
    minReplicas: 1
    maxReplicas: 4
    scaleCooldownSeconds: 60
  gpuA10:
    minReplicas: 0
    maxReplicas: 4
  gpuH100:
    minReplicas: 0
    maxReplicas: 2

monitoring:
  enabled: true
  grafana:
    adminPassword: admin
  alertmanager:
    slack:
      webhookUrl: ""        # 留空 = dev-null receiver
      channel: "#slurm-alerts"

ha:
  operatorReplicas: 2      # Leader Election 模式
```

### 目前已有什麼可以直接 Helm 化
- StatefulSet 的 replica 數、image tag、resource request 已全部從 `worker-pools.json` 派生 → 可改成 `values.yaml`。
- `PARTITIONS_JSON` env var 已是 JSON 字串，可用 Helm `toJson` filter 注入。
- Prometheus alert rules ConfigMap 是純 YAML → 直接進 `templates/`。

### 交付形式
發布到 OCI registry（`ghcr.io/SoWiEee/slurm-on-k8s`），讓使用者可以：
```bash
helm repo add slurm-on-k8s https://SoWiEee.github.io/Slurm-on-K8s-For-DDP/charts
helm install my-cluster slurm-on-k8s/slurm-on-k8s -f my-values.yaml
```

---

## 5-B：OpenTelemetry 分散式追蹤

### 為什麼 metrics 不夠
Prometheus 告訴你「現在 p95 provisioning latency 是 45 秒」，但不告訴你：
- 這 45 秒是花在 K8s 排程（pending pod）、image pull、還是 Slurm node registration？
- 是某個特定 job 特別慢，還是系統性問題？

OpenTelemetry trace 回答的是「**這一次** job J42 為什麼比較慢」。

### Trace 結構設計

```
TraceID: job-{SLURM_JOB_ID}
│
├── [Span] job_submit
│     attributes: job_id, partition, user, requested_nodes, requested_cpus
│
├── [Span] queue_wait  (start=submit_time, end=scale_up_decision_time)
│     attributes: pending_jobs_at_submit, pool
│
├── [Span] scale_up_decision  (operator)
│     attributes: from_replicas, to_replicas, reason
│
├── [Span] k8s_provisioning  (start=patch_time, end=pods_ready_time)
│     attributes: pool, target_replicas
│     → 已有資料來源：slurm_operator_provisioning_latency_seconds histogram
│
├── [Span] slurm_node_registration
│     attributes: node_name, registered_at
│
├── [Span] job_running  (start=start_time, end=end_time)
│     attributes: nodes, cpus, gres
│
└── [Span] checkpoint_write  (可選，若 SIGTERM handler 發出)
      attributes: checkpoint_path, file_size_bytes
```

### 實作路徑
1. **Operator 加 OTel SDK**：`opentelemetry-sdk` + `opentelemetry-exporter-otlp`，在 scale_up/scale_down 決策處建立 Span。
2. **Exporter 加 Span**：每次 slurmrestd 呼叫包在 Span 裡，紀錄 HTTP latency。
3. **OTel Collector sidecar**：在 monitoring namespace 部署 `otel/opentelemetry-collector`，接收 OTLP，轉發到 Jaeger（dev）或 Grafana Tempo（prod）。
4. **Grafana Tempo 整合**：已有 Grafana → 加 Tempo datasource → exemplar 連結（從 Prometheus histogram 直接跳到對應 Trace）。

### Exemplar 連結（killer feature）
Prometheus histogram 支援 exemplar，讓 Grafana 可以：
- 在 Provisioning Latency p95 的時間軸上，顯示「這個 spike 對應 job-123 的 TraceID」
- 點一下直接跳到 Jaeger/Tempo 看整條 trace

這是「metrics → traces」的橋接，目前沒有任何 Slurm-on-K8s 方案做到。

---

## 5-C：Fair-Share 多租戶

### 目標 TA
學術單位、企業 AI 平台：**多個研究小組共用 GPU 叢集，但每組有不同的優先權和配額**。

### Slurm Fair-Share 機制
Slurm 的 Fair-Share Scheduler 基於每個 account 的累積使用量（`RawUsage`）和配額（`shares`）計算一個優先分數 `FairShare ∈ [0, 1]`：
```
FairShare = 1 - (用量 / 配額)
FairShare 越高 → 優先排程
```

### 實作計畫

**Slurm 側設定（sacctmgr）：**
```bash
sacctmgr add account ai-team1 parent=root fairshare=100
sacctmgr add account ai-team2 parent=root fairshare=50   # 給 ai-team1 兩倍優先權
sacctmgr add user team1-user account=ai-team1
```

**Exporter 新增 metrics（`sshare` REST API）：**
```
slurm_account_fairshare{account="ai-team1"}   → 0.83
slurm_account_raw_usage_cpu_hours{account="ai-team1"}
slurm_account_pending_jobs{account="ai-team1"}
```

**Grafana 新 Dashboard「Fair-Share & Accounting」：**
- Bar gauge: 各 account 的 FairShare 分數（顯示誰快被懲罰了）
- Stacked area: 各 account 的 CPU-hours 累積使用趨勢
- 表格: 各 account 目前 pending / running jobs

**Operator 延伸：**
- Scale-up 時可優先考慮 FairShare 分數高的 pool（通常對應高優先 account）
- 新指標 `slurm_operator_scale_up_total` 加 `account` label

### TA 的使用場景
- 大學 AI 系：研究所 A 跑了 3 天的 LLM fine-tuning，FairShare 下降 → 研究所 B 的 job 自動排在前面
- 企業內部：商業部門（高 shares）的訓練 job 比研究部門優先

---

## 5-D：Operator 高可用（HA）

### 問題
目前 Operator 是 `replicas: 1`。Pod 重啟（升級、節點驅逐）會造成 15–30 秒的決策空窗：
- 這段時間 pending jobs 不會觸發 scale-up。
- 更嚴重的是 scale-down cooldown 計時器在記憶體中，重啟後歸零 → 可能在 cooldown 期間觸發不應該的縮容。

（Cooldown 已用 StatefulSet annotation 持久化，部分緩解第二個問題，但仍有啟動後的第一次 loop 判斷異常視窗。）

### Leader Election 設計

使用 K8s `coordination.k8s.io/v1` Lease 物件做 Leader Election：

```python
# operator/__init__ 加入
from kubernetes import client, config

lease_name = "slurm-operator-leader"
lease_namespace = "slurm"

def acquire_lease() -> bool:
    """Try to become leader. Return True if successful."""
    ...
```

實作上可用 `kubernetes-client/python` 的 `LeaderElector`，或等效的 `kubectl` CLI：
```bash
# 現有架構不引入 python k8s client SDK，用 annotation 模擬 lease
kubectl -n slurm annotate lease slurm-operator-leader \
  "leader-id=$(hostname)" --overwrite
```

**狀態機：**
```
standby → try_acquire_lease → leader → lost_lease → standby
                                  ↓
                           scaling loop runs
```

**部署設定：**
```yaml
spec:
  replicas: 2
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0   # 確保至少一個 leader 在線
      maxSurge: 1
```

新指標 `slurm_operator_is_leader{pod="operator-xxx"} = 1/0`，讓 Grafana 可視化哪個 pod 是當前 leader。

### HA 後的 Operator 升級流程
```
1. kubectl set image deployment/slurm-elastic-operator ...
2. 新 Pod 啟動 → standby（等待 lease）
3. 舊 Pod 正常 shutdown → release lease
4. 新 Pod 取得 lease → 成為 leader
5. 零停機升級完成
```

---

## Debug Record

### 問題 1：slurmdbd 映像缺少 slurmdbd 套件

**現象：** `slurmdbd` pod CrashLoopBackOff，log 顯示 `/bin/bash: line 23: exec: slurmdbd: not found`。

**原因：** `slurm-accounting.yaml` 使用 `slurm-controller:latest` 映像跑 slurmdbd，但 controller Dockerfile 只安裝了 `slurmctld`、`slurmd`、`slurmrestd`，沒有裝 `slurmdbd` 套件（Ubuntu 22.04 的 `slurmdbd` 是獨立套件）。

**修法：** `docker/controller/Dockerfile` 加入 `slurmdbd`，重新 build + kind load。

---

### 問題 2：slurmdbd 啟動後 hostname 不符

**現象：** slurmdbd 啟動後立即 fatal exit：`This host not configured to run SlurmDBD (slurmdbd-xxx != slurmdbd)`。

**原因：** `slurmdbd.conf` 的 `DbdHost=slurmdbd`，但 Deployment pod 的 hostname 是 `slurmdbd-{replicaset}-{random}`（Kubernetes 預設行為）。slurmdbd 在啟動時會驗證 `DbdHost` 是否匹配當前 hostname。

**修法：** `slurm-accounting.yaml` 的 Deployment pod spec 加入 `hostname: slurmdbd`，讓 pod hostname 固定為 `slurmdbd`。

---

### 問題 3：slurmctld 首次啟動 fatal（TRES 缺失）

**現象：** 新叢集第一次啟動時，slurmctld fatal exit：`You are running with a database but for some reason we have no TRES from it`。

**原因：** `slurm.conf` 設定了 `AccountingStorageType=accounting_storage/slurmdbd`，slurmctld 啟動時需要從 slurmdbd 取得 TRES（Trackable RESources）定義。若 slurmdbd 尚未 ready（容器剛建立），且又沒有本地 state file，slurmctld 就會 fatal exit 而非等待。

**修法：** `scripts/render-core.py` 的 controller 啟動腳本加入 wait loop：偵測到 `AccountingStorageType=slurmdbd` 時，先用 bash TCP 連線確認 `slurmdbd.slurm.svc.cluster.local:6819` 可達，再 exec slurmctld。

```bash
if grep -q 'AccountingStorageType=accounting_storage/slurmdbd' /etc/slurm/slurm.conf; then
  until (echo >/dev/tcp/slurmdbd.slurm.svc.cluster.local/6819) 2>/dev/null; do sleep 3; done
fi
exec slurmctld -Dvvv
```

### 問題 4：PDB 與 StatefulSet 縮容的關係

**常見誤解：** 認為 PDB 的 `maxUnavailable: 1` 會阻止 operator 把 replicas 從 4 降到 0。

**實際行為：**
- StatefulSet `replicas` 調整是 **Desired State**，K8s controller 會逐步刪除 Pod（最高優先）
- PDB 保護的是 **Voluntary Disruption**（如 `kubectl drain node`、節點升級）
- operator 調整 replicas = K8s 內部操作，**不受 PDB 約束**
- 結論：PDB 與 drain-then-scale 並不衝突；PDB 保護的是基礎設施層面，drain 保護的是 job 層面

---

### 問題 5：`MpiDefault=pmi2` 與 `mpi_pmi2.so` plugin 位置

**現象：** 改為 `MpiDefault=pmi2` 後，`srun --mpi=pmi2` job 可能出現 `srun: error: PMI2 not found`。

**原因：** Ubuntu 22.04 的 `slurmd` 套件把 MPI plugin 放在 `/usr/lib/x86_64-linux-gnu/slurm-wlm/`，路徑需在 `PluginDir` 中。

**排查步驟：**
```bash
# 在 worker pod 確認 pmi2 plugin 存在
kubectl -n slurm exec pod/slurm-worker-cpu-0 -- \
  find /usr/lib -name 'mpi_pmi2.so' 2>/dev/null

# 確認 PluginDir 設定（通常不用手動設）
kubectl -n slurm exec pod/slurm-controller-0 -- \
  scontrol show config | grep PluginDir
```

**實際發現：** Ubuntu 22.04 `slurmd`（Slurm 21.08）內建 PMI2，不需要額外安裝；plugin 會自動在 PluginDir 找到。

---

### 問題 6：`srun --mpi=pmi2` 在單節點多 task 的行為

**確認：** `--ntasks=2 --nodes=1` 加上 `srun --mpi=pmi2` 可以在同一個 worker pod 啟動兩個 MPI rank，`$SLURM_PROCID` 分別為 0 和 1。這對容器化 HPC 測試是最低門檻的 MPI 驗證方式，不需要 pod 間網路或 InfiniBand。

---

### 問題 7：`/etc/profile.d/slurm-modulepath.sh` 在 sbatch 裡不生效

**現象：** login pod 互動式 shell `module avail` 正常，但 sbatch job 內 `module load` 後 `MPI_HOME` 仍是 NOT_SET。

**原因：**
- `/etc/profile.d/*.sh` 只在 **login shell** 啟動時自動 source（`bash -l`）
- Slurm 以非互動、非 login 的 `/bin/bash` 執行 sbatch 腳本
- 因此 `/etc/profile.d/slurm-modulepath.sh` 完全沒被讀到，`MODULEPATH` 未設定
- Lmod 找不到 `/opt/modulefiles`，`module load openmpi/4.1` 靜默失敗

**修法：** 改用 Lmod 官方機制，在 Dockerfile 寫入 `/etc/lmod/modulespath`：
```dockerfile
RUN mkdir -p /etc/lmod && echo '/opt/modulefiles' > /etc/lmod/modulespath
```
Lmod 在每次 `source /etc/profile.d/lmod.sh` 時都會讀這個檔案，不論 shell 類型。

---

### 問題 8：job output 在 worker pod，不在 login pod

**現象：** `cat /tmp/phase5-verify-$jid.out` 在 login pod 找不到檔案。

**原因：** Slurm 的 `--output` 路徑是在**執行 job 的 worker node** 上建立的。
沒有共享 filesystem（NFS/Lustre），output 不會自動傳回 login node。

**在真實 HPC：** 所有節點共享 NFS，`/home/user/` 或 `/scratch/` 上的 output 到處都能讀。

**最終解法（Lmod + NFS 整合）：** `bootstrap-lmod.sh` 掛載 NFS 到所有 Pod，並確保 `/shared/jobs/` 目錄存在。job script 輸出路徑改為：
```bash
#SBATCH --output=/shared/jobs/phase5-verify-%j.out
#SBATCH --error=/shared/jobs/phase5-verify-%j.err
```
所有節點共享同一 NFS，login pod 可直接 `cat /shared/jobs/<outfile>` 取得輸出。

---

### 問題 9：Phase 3 E2E — slurmd 在新 pod 啟動後持續 NOT_RESPONDING

**影響範圍：** `verify-storage-e2e.sh` 的多節點 sbatch 驗證流程。

**現象：** operator 把 worker 從 1 台 scale-up 到 2 台後，`slurm-worker-cpu-1` pod 已 Running，但 `sinfo` 一直顯示 `idle*`（IDLE+NOT_RESPONDING）。SIGHUP 讓它短暫變成 `idle`，但 sbatch job 跑完卻卡在 COMPLETING 數分鐘不離開 queue。

**根因（三層疊加）：**

| 層次 | 現象 | 原因 |
|------|------|------|
| 1. NP race | pod 起來後 ~30s 內 `idle*` | CNI NetworkPolicy 比 slurmd 第一次 registration RPC 晚幾秒套用；slurmctld 的 back-ping（registration agent）此時失敗 → NOT_RESPONDING |
| 2. Slurm 扇出 RPC 使用 ephemeral port | RESPONSE_FORWARD_FAILED，心跳持續失敗 | slurmctld 的 fan-out tree RPC 要求 worker 連回 controller 的**臨時 port**（OS 隨機分配，非 6817）回傳聚合結果。NetworkPolicy `allow-worker-egress` 只開放 6817，其餘封包被 drop |
| 3. slurmctld IP cache 過期 | TERMINATE_JOB Connection timed out，COMPLETING 永遠不解 | pod 每次重啟取得新 IP（e.g. .44 → .84 → .91），但 slurmctld 把 NodeAddr 解析結果快取在記憶體，不重查 DNS；所有後續 PING/TERMINATE_JOB/KILL_JOB 打到舊 IP |

**診斷方式：**
```bash
# 看 slurmctld 在用哪個 IP 連 cpu-1
kubectl -n slurm logs pod/slurm-controller-0 | grep "connect to.*6818"
# 對比 pod 實際 IP
kubectl -n slurm get pod slurm-worker-cpu-1 -o jsonpath='{.status.podIP}'

# 看 slurmd 收到 zero-bytes 與 RESPONSE_FORWARD_FAILED
kubectl -n slurm logs pod/slurm-worker-cpu-1 | grep -E "Zero Bytes|FORWARD_FAILED|slurm_msg_sendto"
```

**修法（三步對應三層）：**

1. **NetworkPolicy 放開 worker → controller 所有 port**（解決 ephemeral port 被 block）

   `manifests/networking/network-policy.yaml` 的 `allow-worker-egress` 和 `allow-login-egress` 中，原本 `ports: [6817, 22]` 改為**不限制 port**（移除 ports 欄位），允許 worker/login 往 controller 送任何 TCP。

2. **SIGHUP 補充 registration**（解決 NP race）

   `verify-storage-e2e.sh` 的 `wait_slurm_node_responding()` 在 `idle*` 持續 30 秒後，對 worker pod 發 `kill -HUP $(pgrep slurmd)`，觸發 slurmd 重新送 registration RPC；此時 NP 已套用完成，back-ping 成功。

3. **更新 slurmctld 的 NodeAddr**（解決 IP cache 過期）

   在 `wait_slurm_node_responding()` 開始前（以及 SIGHUP 後）執行：
   ```bash
   pod_ip=$(kubectl -n slurm get pod slurm-worker-cpu-1 -o jsonpath='{.status.podIP}')
   kubectl -n slurm exec pod/slurm-controller-0 -- bash -lc \
     "scontrol update NodeName=slurm-worker-cpu-1 NodeAddr=${pod_ip}"
   ```
   強制 slurmctld 更新快取 IP，之後的 PING/TERMINATE_JOB 才能到達新 pod。

**附帶發現：**
- `sbatch --hold` 讓 trigger job 保持 PENDING 不執行；`scancel` 已 hold 的 job 瞬間消失（無 COMPLETING 殘留），比取消 RUNNING job 乾淨很多。
- 多節點 job 的 COMPLETING 卡住，主因是 `REQUEST_TERMINATE_JOB`（slurmctld→slurmd:6818）或 `EPILOG_COMPLETE`（slurmd→slurmctld:6817）任一方向失敗，不是 NFS 問題。
- `scontrol reconfigure` **不應**在 verify 流程呼叫：它會讓 slurmctld 嘗試 DNS resolve 所有靜態節點（包含未部署的 gpu 節點），Block 30–60 秒，導致 operator REST API 回傳空結果，誤觸 scale-down。

---

## Phase 5 建議優先順序

| 項目 | 難度 | TA 價值 | 建議順序 |
|------|------|---------|---------|
| Helm Chart | 中 | 所有 TA（部署門檻決定採用率） | 第一 |
| Fair-Share metrics | 低 | AI 平台 TA（可視化多租戶） | 第二（與 Helm 並行） |
| Operator HA | 中 | SRE / 生產環境 TA | 第三 |
| OpenTelemetry | 高 | 所有 TA（差異化觀測） | 第四 |

Helm 和 Fair-Share metrics 可以平行進行（互不依賴），HA 需要先完成 Helm（讓 `replicas` 從 values 控制），OTel 放最後因為需要最多程式碼改動。

---

## 設計重點

**為什麼 Slurm node 要預先全部宣告？**
所有節點在 `slurm.conf` 裡預先定義到 `maxNodes`，Operator 只調整 StatefulSet 的 replica 數，而不重寫 Slurm 設定檔。這避免了每次擴縮時 `slurmctld` 重新解析所有 DNS 造成的連鎖延遲。

**為什麼 Operator 不用 Kopf 或 CRD？**
刻意保持輕量。Operator 是純 Python，沒有自訂 CRD、沒有 webhook，部署門檻低，邏輯一眼就能看懂。Slurm 狀態查詢（queue、job、node）透過 slurmrestd REST API 進行；StatefulSet replica 調整仍透過 `kubectl patch`。

**Checkpoint Guard + Drain-then-Scale 是什麼？**
縮容分兩個階段保護執行中的 AI 訓練任務：

1. **Checkpoint Guard**：若 checkpoint 檔案不存在或超過 `MAX_CHECKPOINT_AGE_SECONDS`（預設 10 分鐘），縮容決策會被阻擋。`CHECKPOINT_PATH=""` 時自動停用（避免靜默 block）；`CHECKPOINT_GRACE_SECONDS` 允許 job 啟動初期尚未寫出 checkpoint 時仍可縮容（grace period 內不阻擋）。
2. **Drain-then-Scale**：通過 Guard 後，Operator 先呼叫 `scontrol update State=DRAIN` 將目標節點標記為不接受新 job，等到 `CPUAlloc == 0`（節點上所有 job 都跑完）才真正減少 StatefulSet replica，避免執行中的訓練被強制中斷。若在 drain 等待期間有新的 scale-up 需求，drain 會自動取消（`State=RESUME`）。

**Operator Cooldown 如何在 Pod 重啟後存活？**
每次 scale-up 成功後，Operator 把時間戳寫入 StatefulSet annotation `slurm.k8s/last-scale-up-at`。Pod 重啟時從 annotation 還原 cooldown 計時，避免重啟後立即觸發錯誤的縮容。

**為什麼 slurmctld 不怕 pod 重啟？**
`StateSaveLocation`（`/var/spool/slurmctld`）掛載了獨立的 PVC（`slurm-ctld-state`）。controller pod 重啟後，job queue、node 狀態、及會計紀錄指標都會從磁碟還原，不需要重新提交任務。

**slurmdbd 提供什麼？**
slurmdbd 搭配 MySQL 後端，把每個 job 的 CPU-hours、使用者、帳戶等資訊持久化。`sacct` 可以查詢歷史 job 統計，也是 Phase 5 Fair-Share 多租戶排程的前置條件。

# Job-Hardware Mapping

## 📦 資源模型概覽

本 repo 採用 **Slurm-on-K8s 雙層架構**，每個 worker node 是一個 K8s Pod，Slurm 把整個 Pod 視為一台 node：

```
Slurm Layer  ←─ 排程、配置記帳（CR_Core、GRES）
     ↓
K8s Layer    ←─ 容器資源請求（requests/limits）、device plugin
     ↓
Hardware     ←─ 實體 CPU cores、GPU SM + VRAM
```

關鍵設定（`slurm.conf`）：

| 參數 | 值 | 意義 |
|------|----|------|
| `SelectType` | `select/cons_tres` | CPU/GPU 皆以 consumable resource 模式分配 |
| `SelectTypeParameters` | `CR_Core` | Slurm 以 core 為單位追蹤 CPU 消耗 |
| `TaskPlugin` | `task/none` | **無 cgroup/CPU binding**，排程計帳但不強制隔離 |
| `GresTypes` | `gpu` | GPU 宣告為 GRES |

每台 worker 宣告：CPU worker → `CPUs=4`；GPU worker → `CPUs=4` + `Gres=gpu:<type>:1`

---

## 📍 部署環境規格：Linux + k3s + RTX 5070

> 本節以實際遷移目標環境為例，說明硬體資源如何對應到各層宣告。

### 主機硬體

| 項目 | 規格 | 說明 |
|------|------|------|
| CPU | 12 cores（如 Intel i7-12700 / Ryzen 9 5900X） | k3s 單節點，所有 pod 共用 |
| RAM | 32 GB DDR5 | 各 worker pod 依 `RealMemory` 宣告分配帳本 |
| GPU | NVIDIA RTX 5070（Blackwell GB203） | 1 張，連入 `/dev/nvidia0` |
| GPU VRAM | 12 GB GDDR7 | 單一作業可用全部 VRAM |
| GPU SM | 48 個 Streaming Multiprocessors | MPS 可按百分比分配 SM |
| GPU CUDA Cores | 6144 | Blackwell 架構 |
| 記憶體頻寬 | ~672 GB/s | |
| OS / Runtime | Ubuntu 22.04 + containerd + k3s | `K8S_RUNTIME=k3s` |

### Worker Pod 資源宣告對照

在 k3s 單節點上，所有 worker pod 實際跑在同一台實體主機。Slurm 帳本追蹤的是「pod 宣告量」，OS 層透過 **cgroup v2**（`TaskPlugin=task/cgroup`，`REAL_GPU=true`）做實際隔離。

| Worker 類型 | StatefulSet 名稱 | Slurm CPUs | Slurm RealMemory | GRES | K8s resource.limits |
|------------|----------------|-----------|-----------------|------|---------------------|
| CPU worker | `slurm-worker-cpu` | 4 cores | 3500 MB (~3.4 GB) | 無 | cpu: 4, memory: 3500Mi |
| GPU worker | `slurm-worker-gpu-rtx5070` | 4 cores | 3500 MB | gpu:rtx5070:1 | cpu: 4, memory: 3500Mi, nvidia.com/gpu: 1 |

> 因為只有 1 張 RTX 5070，GPU worker pool 的 `maxNodes=1`（只能開 1 個 GPU pod）。CPU worker pool 最多可開 4 個 pod（受主機可用資源限制）。

### 工作類型與資源分配

#### Type 1：CPU 批次訓練（data preprocessing / feature engineering）

```bash
#SBATCH --job-name=preprocess
#SBATCH --ntasks=2
#SBATCH --cpus-per-task=2   # 共請求 4 cores
#SBATCH --mem=2G
#SBATCH --constraint=cpu
```

**分配流程：**
```
提交 → slurmctld 掃描 cpu-worker 帳本
     → slurm-worker-cpu-0 剩餘 4 slots，符合需求
     → 分配 slot 0-3（2 task × 2 cores）
     → cgroup v2 限制 pid 只跑在指定 cpu set
     → Job 執行，2 個 srun task 各佔 2 cores
```

| 資源 | 宣告 | 實際強制 |
|------|------|---------|
| CPU | 4 cores (2×2) | ✅ cgroup cpuset 限制 |
| RAM | 2 GB | ✅ cgroup memory limit |
| GPU | 無 | - |

#### Type 2：GPU 單卡訓練（PyTorch 單機單卡）

```bash
#SBATCH --job-name=train-single-gpu
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=3G
#SBATCH --gres=gpu:rtx5070:1
#SBATCH --constraint=gpu-rtx5070
```

**分配流程：**
```
提交 → slurmctld 查 GRES 帳本：gpu:rtx5070
     → slurm-worker-gpu-rtx5070-0 有 1 個空閒 GPU slot
     → GRES 計數 1→0，worker 滿載（整卡獨占）
     → K8s device plugin 把 /dev/nvidia0 綁入 pod
     → CUDA_VISIBLE_DEVICES=0，SLURM_JOB_GPUS=0
```

| 資源 | 宣告 | 實際可用 |
|------|------|---------|
| CPU | 4 cores | cgroup 保障 |
| RAM | 3 GB | cgroup 保障 |
| GPU VRAM | 12 GB（整卡） | 全部 VRAM 可用 |
| GPU SM | 48 SM（100%） | 所有 SM 獨占 |

#### Type 3：MPS 多工推論（多個小型推論服務共用 RTX 5070）

MPS 讓多個 job 在同一張 GPU 的 SM 上**真正並行**（非時間片輪流）。

```bash
# Job A（LLM serving，要求 50% SM）
#SBATCH --gres=mps:50
#SBATCH --mem=1G

# Job B（image classifier，要求 25% SM）
#SBATCH --gres=mps:25
#SBATCH --mem=1G

# Job C（embedding server，要求 25% SM）
#SBATCH --gres=mps:25
#SBATCH --mem=1G
```

**分配流程：**
```
MPS Daemon（DaemonSet）已在 GPU 節點啟動，佔用 /dev/nvidia0
     ↓
Job A 提交 → mps slot 50 分配 → CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=50
Job B 提交 → mps slot 25 分配 → CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25
Job C 提交 → mps slot 25 分配 → CUDA_MPS_ACTIVE_THREAD_PERCENTAGE=25
     ↓
三個 job 同時跑，MPS Server 統一調度：
  A 佔 ~24 SM（50%），B 佔 ~12 SM（25%），C 佔 ~12 SM（25%）
  總計 48 SM 滿載，GPU 利用率最大化
```

| 資源 | Job A | Job B | Job C |
|------|-------|-------|-------|
| CPU | 共用 cpu-worker（另行排程） | 同左 | 同左 |
| GPU SM | ~24 SM（50%） | ~12 SM（25%） | ~12 SM（25%） |
| GPU VRAM | **共享 12 GB**（無隔離） | 同左 | 同左 |

> ⚠️ 三個 job 共用 12 GB VRAM，無硬體隔離。如果 Job A OOM，可能影響 B 和 C。適合**可信任的同一使用者**提交的多個推論服務。

#### Type 4：單節點多工序 DDP（PyTorch DataParallel on 1 GPU）

RTX 5070 是唯一的 GPU，無法跨多 GPU 做真正的 DDP（multi-GPU DDP 需要多張卡）。但可以在單節點單 GPU 上跑 PyTorch `DataParallel`（DP）或單 process 訓練：

```bash
#SBATCH --job-name=dp-training
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=3G
#SBATCH --gres=gpu:rtx5070:1
```

多 GPU DDP 場景（若未來擴充多張 GPU 的 Linux 主機）：

```bash
# 假設未來有 4 台 GPU worker，各持 1 張 RTX 5070
#SBATCH --nodes=4
#SBATCH --ntasks-per-node=1
#SBATCH --gres=gpu:rtx5070:1
#SBATCH --constraint=gpu-rtx5070
# → torchrun --nproc_per_node=1 --nnodes=4 train.py
# → NCCL AllReduce 流量走 K8s pod network
```

### 資源帳本總覽（單節點部署）

```
實體主機 (Linux + k3s)
├── CPU: 12 physical cores, 32 GB RAM
│   ├── slurm-controller-0       (系統服務 pod)
│   ├── slurm-worker-cpu-0       → Slurm 宣告 CPUs=4, Mem=3.4G
│   ├── slurm-worker-cpu-1       → 同上（依作業需求由 operator 開啟）
│   ├── slurm-worker-cpu-2       → 同上
│   ├── slurm-worker-cpu-3       → 同上
│   └── slurm-worker-gpu-rtx5070-0 → Slurm 宣告 CPUs=4, Mem=3.4G, Gres=gpu:rtx5070:1
│
└── GPU: RTX 5070 /dev/nvidia0
    └── 整卡給 slurm-worker-gpu-rtx5070-0（或 MPS 模式下分成多個 slot）
```

> **k3s vs Kind 差異：** k3s 直接使用 containerd + NVIDIA runtime，不需要 container-in-container；`/dev/nvidia0` 直接可見，`TaskPlugin=task/cgroup` 有真實 OS 隔離效果。Kind 在 Windows 下無法直通 GPU，所有 GPU 宣告為 `File=/dev/null`（排程帳本有效，實際不存在硬體）。

---

## CPU Job 分配

### 排程語意：同一 worker 可容納多個 job

`cons_tres` + `CR_Core` 使 Slurm 以 core 為單位消耗 CPU slot。同一台 node 的 CPU slot 可由多個 job 分攤，只要總需求不超過宣告量。這是 HPC 常見的 **bin-packing** 行為，無需 `OverSubscribe`。

> `OverSubscribe` 是讓多個 job「共用同一批 CPU」（超賣）；**bin-packing 是不同 job 用不同 CPU slot**，兩個概念不同。

### 分配範例（CPUs=4 的 cpu-worker）

| Job | 請求 | 分配在同一 worker？ | 說明 |
|-----|------|-------------------|------|
| A `--cpus-per-task=2` | 2 cores | ✅ | 佔用 slot 0-1 |
| B `--cpus-per-task=2` | 2 cores | ✅ 與 A 共存 | 佔用 slot 2-3，worker 滿載 |
| C `--cpus-per-task=1` | 1 core | ❌ 排到其他 worker | worker 已滿，Slurm 等待或排第二台 |

更複雜的例子——4 個 job 競搶 2 台 worker（各 CPUs=4）：

```
Worker-0 [4 slots]         Worker-1 [4 slots]
┌─────────────────┐        ┌─────────────────┐
│ Job A (2 cores) │        │ Job C (3 cores) │
│ Job B (2 cores) │        │ Job D (1 core)  │
└─────────────────┘        └─────────────────┘
  bin-packed: 100%            bin-packed: 100%
```

Job A、B 同時跑在 Worker-0；Job C、D 同時跑在 Worker-1。Slurm 的 backfill scheduler 會盡量填滿 slot。

### 邊界：隔離層缺失

`TaskPlugin=task/none` + `ProctrackType=proctrack/linuxproc` 表示：

- Slurm **記帳**說分給 Job A 2 cores，但 OS 不會強制 Job A 只跑在那 2 顆上
- 若應用程式內部開 `OMP_NUM_THREADS=16`，它可能吃掉 Worker 上所有 CPU，影響同 worker 的 Job B
- **效能隔離在 Kind 環境是虛的**，僅排程語意層面有效

真實 HPC 環境通常改用 `TaskPlugin=task/cgroup`，搭配 `CgroupAutomount=yes`，OS 層級強制 CPU pinning。

---

## GPU Job 分配

### 預設模型：整卡獨占

每台 GPU worker 宣告 1 個 GRES slot（如 `Gres=gpu:rtx5070:1`）。GRES 是整數消耗，無分數分配：

```
# Linux + k3s + RTX 5070 環境（maxNodes=1，只有 1 台 GPU worker）
Job A: --gres=gpu:rtx5070:1  →  佔用整張 RTX 5070，該 worker GRES=0
Job B: --gres=gpu:rtx5070:1  →  Pending，等 Job A 釋放（因為只有 1 台 GPU worker）
```

> **Kind（Windows 開發環境）下**：GPU worker 宣告 `File=/dev/null`，排程帳本上是 1 個 GPU slot，但實際不存在硬體；用來驗證 Slurm 路由邏輯。

### 分配範例

**場景 A：Linux + k3s + 1 台 RTX 5070 worker（實際部署）**

| Job | 請求 | 分配結果 |
|-----|------|---------|
| A `--gres=gpu:rtx5070:1 -N 1` | 1× RTX 5070 | → worker-gpu-rtx5070-0，整張 12 GB 獨占 |
| B `--gres=gpu:rtx5070:1 -N 1` | 1× RTX 5070 | → Pending，只有 1 台 GPU worker，等 A 結束 |

**場景 B：假設未來多張 GPU 機器（可參考設計）**

| Job | 請求 | 分配結果 |
|-----|------|---------|
| A `--gres=gpu:rtx5070:1 -N 1` | 1× GPU | → worker-gpu-rtx5070-0，整張獨占 |
| B `--gres=gpu:rtx5070:1 -N 1` | 1× GPU | → worker-gpu-rtx5070-1，整張獨占 |
| C `--gres=gpu:rtx5070:1 -N 1` | 1× GPU | → Pending，等 A 或 B 釋放 |

多節點 DDP job（多 GPU worker 情境）：

```bash
#SBATCH --gres=gpu:rtx5070:1
#SBATCH -N 4          # 需要 4 台 GPU worker 同時空閒
#SBATCH --ntasks-per-node=1
```

Slurm 要求 4 台 `gpu-rtx5070` worker 同時空閒。這正是 Gang Scheduling 解決的問題——若只有 3 台空閒，K8s 1.35 原生 `GangScheduling` 會讓 4 個 worker Pod 要嘛全部調度，要嘛全不調度，避免佔著資源等人。

### GPU 共用機制（進階）

若要讓多個 job 共用同一張 GPU，需要額外機制：

#### Time-Slicing（時間切片）

CUDA context 輪流使用 GPU，類似 CPU 分時多工。

- 適用：**所有 NVIDIA GPU**
- 隔離：**無記憶體隔離**（所有 context 共享 VRAM），context switch 有開銷
- K8s 設定：GPU Operator ConfigMap 把 1 張 GPU 虛擬成 N 份 `nvidia.com/gpu`

```yaml
# ConfigMap：1 張 RTX 5070 虛擬成 4 份（需 GPU Operator，本架構未使用）
sharing:
  timeSlicing:
    resources:
    - name: nvidia.com/gpu
      replicas: 4
```

```ini
# gres.conf（Slurm 端）
NodeName=slurm-worker-gpu-rtx5070-0 Name=gpu Type=rtx5070 Count=4 File=/dev/nvidia0
```

4 個 job 可同時各請求 `--gres=gpu:rtx5070:1`，時間輪流使用同一張 GPU。**不適合 DDP 訓練**（延遲不可預測、無記憶體保護）。

#### MIG（Multi-Instance GPU）— 硬體分割

A100 / H100 / A30 支援在**硬體層**將 GPU 切成獨立 instance，各有專屬 SM、L2 cache、VRAM 帶寬，完全隔離。

| Profile | SM | VRAM | 每張 A100 80GB 可建 |
|---------|----|------|-------------------|
| `1g.10gb` | 1/7 GPU | 10 GB | 最多 7 個 |
| `2g.20gb` | 2/7 GPU | 20 GB | 最多 3 個 |
| `3g.40gb` | 3/7 GPU | 40 GB | 最多 2 個 |
| `7g.80gb` | 完整 GPU | 80 GB | 1 個（無分割）|

```ini
# gres.conf（MIG 模式，僅供參考，不適用於 RTX 5070）
NodeName=slurm-worker-gpu-h100-0 Name=gpu Type=mig-2g.20gb Count=3
NodeName=slurm-worker-gpu-h100-0 Name=gpu Type=mig-1g.10gb Count=1
```

Job 請求 `--gres=gpu:mig-2g.20gb:1`，排到任何有空閒 MIG instance 的 node。**僅限 A100/H100/A30；最細粒度 1/7 GPU，仍不能跨 GPU。**

> ❌ **RTX 5070（Blackwell GB203）不支援 MIG**，消費級 GPU 均不支援此功能。

#### MPS（Multi-Process Service）

多個 CUDA process 合併進同一 CUDA context，共享 command queue 和 SM，減少 context switch 開銷。SM 可設 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 比例。適合延遲敏感的小型推論，但無完整記憶體隔離。

---

### GPU MPS 完整實作指南（2026）

#### 本架構環境：Linux + k3s + RTX 5070

> **本架構採用路徑 B（Slurm MPS DaemonSet）**，因為我們使用 k3s + 直接安裝 NVIDIA Container Toolkit，**未部署 NVIDIA GPU Operator**。路徑 A 需要 GPU Operator，在 k3s 上需額外安裝，非必要複雜度。

| 項目 | 規格 |
|------|------|
| GPU | RTX 5070（Blackwell GB203） |
| SM 數量 | 48 SM |
| VRAM | 12 GB GDDR7 |
| MPS 支援 | ✅（Volta+，RTX 5070 符合） |
| MIG 支援 | ❌（消費級 GPU，僅 A100/H100/A30 支援）|
| K8s Runtime | k3s + containerd + NVIDIA Container Toolkit |
| GPU 裝置路徑 | `/dev/nvidia0` |

---

#### MPS 運作原理 vs Time-Slicing

| 維度 | Time-Slicing | MPS | MIG |
|------|-------------|-----|-----|
| 機制 | 多個 CUDA context 輪流使用 GPU（OS 時間片） | 所有 process 共用**同一個** CUDA context，SM 並行執行 | 硬體切分 GPU 為獨立 instance |
| 並行度 | 無（序列執行） | **高**（多 process 真正同時跑在 SM 上） | 有（instance 間獨立） |
| 記憶體隔離 | ❌（VRAM 共享） | ❌（一個 OOM 可能拖垮其他 process） | ✅（各 instance VRAM 隔離） |
| context switch overhead | 高（µs 級別） | **極低**（無 context switch） | 無（instance 獨立） |
| SM 配額控制 | ❌ | ✅ `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` | ✅（profile 固定） |
| 支援 GPU | 全部 NVIDIA | **Volta+（含 RTX 5070）** | A100/H100/A30 only |
| RTX 5070 適用 | ✅ | **✅ 本架構採用** | ❌ 不支援 |
| 最佳場景 | 開發/測試 | **推論服務多 replica 共用 GPU** | 多租戶需記憶體隔離 |

**RTX 5070 + MPS 適合的場景：**
- 多個小型推論 server 並排（LLM serving、image classifier）
- 批次推論（offline batch inference），task 間共享 SM bandwidth
- 教授展示：同一張 GPU 跑多個 AI 服務而不互相等待

**RTX 5070 + MPS 不適合的場景：**
- PyTorch DDP 訓練（DDP 本身已充分利用整張 GPU，加 MPS 只增加風險）
- 需要記憶體隔離的多租戶（RTX 5070 無 MIG，MPS 沒有 VRAM 隔離）
- 不同 Linux 用戶共用同一張 GPU（每個 Linux 用戶只能有一個 MPS server）

---

#### 實作架構：MPS Control Daemon on k3s

```
Linux Host（k3s 單節點）
┌─────────────────────────────────────────────────────┐
│  DaemonSet: nvidia-mps-daemon (namespace: slurm)     │
│  ┌─────────────────────────┐                        │
│  │  mps-control container  │  nvidia-cuda-mps-      │
│  │  image: cuda:12.3-base  │  control -d            │
│  │  hostIPC: true          │  /dev/nvidia0 獨占      │
│  └────────────┬────────────┘                        │
│               │ UNIX socket                         │
│               │ hostPath: /tmp/nvidia-mps/          │
│  ┌────────────┴──────────┐  ┌──────────────────┐   │
│  │  worker-gpu-0 Pod     │  │  (第二個 MPS job  │   │
│  │  slurmd + Job A       │  │   共用同一 pod     │   │
│  │  CUDA_MPS_*=/tmp/mps  │  │   或不同 task)    │   │
│  │  SM 佔 50%            │  └──────────────────┘   │
│  └───────────────────────┘                         │
│  RTX 5070 /dev/nvidia0  (12 GB VRAM, 48 SM)        │
└─────────────────────────────────────────────────────┘
```

Worker pod 掛載 `/tmp/nvidia-mps` hostPath，成為 MPS Control Daemon 的客戶端，所有 CUDA 呼叫由 MPS Server 統一代理分發到 RTX 5070 的 SM。

---

#### 路徑 B 實作步驟（本架構採用，Linux + k3s + RTX 5070）

**前提：** 已執行 `K8S_RUNTIME=k3s REAL_GPU=true bash scripts/bootstrap.sh` 完成基礎部署。

---

**步驟一：確認 NVIDIA 環境**

```bash
nvidia-smi
kubectl get nodes -o custom-columns='NODE:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'
# 期望輸出：GPU 欄位為 "1"（1 張 RTX 5070）
```

---

**步驟二：部署 MPS Control DaemonSet**

已放在 `manifests/gpu/mps-daemonset.yaml`，直接套用：

```bash
# 一鍵部署（推薦）
bash scripts/bootstrap-gpu.sh --with-mps

# 或手動部署
kubectl apply -f manifests/gpu/mps-daemonset.yaml
kubectl -n slurm rollout status daemonset/nvidia-mps-daemon

# 驗證 MPS daemon 回應
mps_pod=$(kubectl -n slurm get pod -l app=nvidia-mps-daemon -o jsonpath='{.items[0].metadata.name}')
kubectl -n slurm exec "pod/${mps_pod}" -- bash -c 'echo get_server_list | nvidia-cuda-mps-control'
```

---

**步驟三：重新 render 帶 MPS mount 的 manifests**

```bash
K8S_RUNTIME=k3s REAL_GPU=true WITH_MPS=true bash scripts/bootstrap.sh
```

`render-core.py --with-mps` 會在 GPU worker StatefulSet 自動加入：

```yaml
# 由 render-core.py 自動注入（WITH_MPS=true）
spec:
  hostIPC: true          # MPS POSIX shared memory 通訊必需
  containers:
  - name: worker
    volumeMounts:
    - name: mps-socket
      mountPath: /tmp/nvidia-mps
  volumes:
  - name: mps-socket
    hostPath:
      path: /tmp/nvidia-mps
      type: DirectoryOrCreate
```

> ⚠️ `hostIPC: true` 讓 pod 存取 host IPC namespace，已受 NetworkPolicy 限制只有 GPU worker pod 才有此設定。

---

**步驟四：gres.conf 設定 MPS slot（RTX 5070）**

在 `slurm.conf`（由 `render-core.py` 生成）加入 MPS GresType：

```ini
# slurm.conf（render-core.py 生成，REAL_GPU=true）
GresTypes=gpu,mps
TaskPlugin=task/cgroup
CgroupPlugin=cgroup/v2
```

```ini
# gres.conf — RTX 5070 宣告 GPU 和 MPS 兩個 GRES 類型
# Count=100 代表 100%（48 SM），可分成任意份
NodeName=slurm-worker-gpu-rtx5070-0 Name=gpu Type=rtx5070 File=/dev/nvidia0 Count=1
NodeName=slurm-worker-gpu-rtx5070-0 Name=mps Count=100
```

每個 job 請求的 `mps:N` 轉換為 N% SM：

| 請求 | 分配 SM（近似） | 剩餘 MPS slot | 適合工作 |
|------|--------------|--------------|---------|
| `--gres=mps:50` | ~24 SM | 50 | 中型推論（7B LLM serving） |
| `--gres=mps:25` | ~12 SM | 75 | 小型推論（image classifier） |
| `--gres=mps:10` | ~5 SM  | 90 | 極輕量 embedding service |
| `--gres=gpu:rtx5070:1` | 48 SM（整卡） | 無共享 | DDP / 大型訓練 |

---

**步驟五：Prolog/Epilog 腳本（可選，精細 SM 控制）**

若希望每個 job 嚴格限制 SM 百分比（掛入 worker container via ConfigMap）：

```bash
# /etc/slurm/prolog.d/99-mps.sh
#!/bin/bash
[[ "${SLURM_JOB_GRES:-}" != *mps* ]] && exit 0
export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
export CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-mps-log
mps_count=$(echo "$SLURM_JOB_GRES" | grep -oP 'mps:\K[0-9]+' || echo 100)
echo "set_active_thread_percentage ${mps_count}" | nvidia-cuda-mps-control
```

```bash
# /etc/slurm/epilog.d/99-mps.sh
#!/bin/bash
[[ "${SLURM_JOB_GRES:-}" != *mps* ]] && exit 0
export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
echo quit | nvidia-cuda-mps-control 2>/dev/null || true
```

---

**步驟六：Job 提交語法**

```bash
# MPS 推論 job（佔用 RTX 5070 的 25% SM = 約 12 SM）
#SBATCH --job-name=infer-mps
#SBATCH --gres=mps:25
#SBATCH --mem=1G
python infer.py --model my_model --batch_size 32
# 最多 4 個這樣的 job 可同時執行，共用 RTX 5070

# 整卡獨占訓練 job（不使用 MPS）
#SBATCH --job-name=train
#SBATCH --gres=gpu:rtx5070:1
#SBATCH --constraint=gpu-rtx5070
#SBATCH --mem=3G
torchrun --nproc_per_node=1 train.py
```

---

**步驟七：驗證**

```bash
# 完整 GPU + MPS 驗證（5 步驟）
bash scripts/verify-gpu.sh

# 手動確認 MPS 狀態
mps_pod=$(kubectl -n slurm get pod -l app=nvidia-mps-daemon -o jsonpath='{.items[0].metadata.name}')
kubectl -n slurm exec "pod/${mps_pod}" -- bash -c '
  echo get_server_list | nvidia-cuda-mps-control
  echo get_client_list | nvidia-cuda-mps-control
'
# 查看 SM 使用率（MPS 下多 job 應同時可見）
kubectl -n slurm exec "pod/${mps_pod}" -- nvidia-smi dmon -s u -d 2
```

---

#### 路徑 A：GPU Operator MPS（參考，本架構未使用）

**適用：** 部署了 NVIDIA GPU Operator 的正式叢集（需要 ClusterPolicy CRD）。

```yaml
# GPU Operator ConfigMap：1 張 RTX 5070 虛擬成 4 個 MPS slot
data:
  any: |
    sharing:
      mps:
        resources:
        - name: nvidia.com/gpu
          replicas: 4
```

```bash
kubectl patch clusterpolicies.nvidia.com/cluster-policy \
  -n gpu-operator --type merge \
  -p '{"spec": {"devicePlugin": {"config": {"name": "mps-config", "default": "any"}}}}'
```

```ini
# gres.conf（對應 replicas: 4）
NodeName=slurm-worker-gpu-rtx5070-0 Name=gpu Type=rtx5070 Count=4 File=/dev/nvidia0
```

---

#### 本架構實作建議

| 部署環境 | 推薦路徑 | 理由 |
|---------|---------|------|
| **Linux + k3s + RTX 5070（本架構）** | **路徑 B（MPS DaemonSet）** | 無 GPU Operator；`bootstrap-gpu.sh --with-mps` 一鍵部署 |
| Kind（Windows 開發） | ❌ 不適用 | Kind 無真實 GPU，hostIPC 無效 |
| 部署了 GPU Operator 的正式叢集 | 路徑 A（GPU Operator MPS） | GPU Operator 處理 daemon 生命週期 |
| 大型 HPC 叢集（多租戶精細 SM 控制） | 路徑 B + Prolog/Epilog | 可按 job 動態調整 SM 百分比 |
| 混合推論+訓練叢集 | 路徑 B + 分 partition | 推論 partition `--gres=mps:N`，訓練 partition `--gres=gpu:rtx5070:1` |

**對 operator/main.py 的影響：**

MPS 模式下 `PARTITIONS_JSON` 裡的 GPU pool 設定需同步更新 GRES 宣告：

```json
{
  "name": "slurm-worker-gpu-rtx5070",
  "match_gres": "gpu:rtx5070",
  "gres_per_node": "gpu:rtx5070:1,mps:100"
}
```

因 RTX 5070 只有 1 張（maxNodes=1），operator 在 MPS 模式下不會 scale-up GPU pool（已有節點），主要幫 CPU pool 做擴縮。縮放邏輯不需修改。

**對 Slurm 設定的影響（render-core.py）：**

```python
# render-core.py 的 gres.conf 生成邏輯（REAL_GPU=true, WITH_MPS=true）
gres_lines.append(f"NodeName={node_name} Name=gpu Type={gpu_type} File=/dev/nvidia0 Count=1")
if args.with_mps:
    gres_lines.append(f"NodeName={node_name} Name=mps Count=100")
```

---

#### 監控 MPS 使用狀況

```bash
# 在 MPS Daemon 所在 pod 或 host 執行
echo "get_server_list" | nvidia-cuda-mps-control    # 查看活躍 server
echo "get_client_list" | nvidia-cuda-mps-control    # 查看連線的 client

# 查看 GPU 利用率（多個 MPS process 應能同時看到 SM 使用）
nvidia-smi dmon -s u

# 用 DCGM 追蹤 MPS 下的 per-process GPU 使用（需 DCGM Exporter）
dcgmi group -c mps-jobs --default
dcgmi dmon -e 203,204,1002   # SM Active, SM Occupancy, Memory Active
```

---

#### 參考來源

- [Slurm GRES MPS 官方文件](https://slurm.schedmd.com/gres.html) — `GresTypes=gpu,mps` 設定與 Prolog 行為
- [GPU Slicing in CycleCloud Slurm with CUDA MPS](https://techcommunity.microsoft.com/blog/azurehighperformancecomputingblog/gpu-slicing-in-cyclecloud-slurm-with-cuda-multi-process-service-mps/4365999) — Microsoft Azure HPC，Slurm Prolog/Epilog 實戰
- [GKE MPS 實作](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/nvidia-mps-gpus) — `hostIPC: true` 要求和 Google 自訂 GPU stack
- [SURF MPS for Slurm GitHub](https://github.com/basvandervlies/surf_slurm_mps) — 荷蘭國家超算中心的 MPS Prolog/Epilog 完整實作
- [NVIDIA GPU Operator MPS 文件](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html) — ClusterPolicy ConfigMap `sharing.mps` 設定
- [MIG vs Time-Slicing vs MPS 比較](https://www.kubenatives.com/p/mig-vs-time-slicing-vs-mps-which) — 三種機制的適用場景分析

### 核心限制：為何不能跨 GPU 分割 SM？

用戶場景：GPU0 剩 4 SM 閒置、GPU1 剩 4 SM 閒置，Job C 需要 8 SM，能否合用？

**直接答案：不可能。** 硬體架構根本限制：

```
  GPU0 [SM0..SM107]                  GPU1 [SM0..SM107]
┌──────────────────┐               ┌──────────────────┐
│  SM0  SM1  ...   │               │  SM0  SM1  ...   │
│  L2 Cache        │               │  L2 Cache        │
│  HBM (80 GB)     │<─PCIe/NVLink─>│  HBM (80 GB)     │
└──────────────────┘               └──────────────────┘
```

1. **無跨 GPU 共享記憶體**：CUDA thread block 必須在同一張 GPU 的 SM 上存取 Shared Memory / L1 cache；跨 GPU 只能用 P2P copy（NVLink/PCIe），延遲是 SM 內 shared memory 的 100 倍以上
2. **CUDA 程式模型不支援**：沒有 API 可讓一個 kernel 跑在「GPU0 SM 0-3 + GPU1 SM 0-3」上
3. MIG / Time-Slicing / MPS 都是**單一 GPU 內**的分割，無法橫跨兩張

「用完碎片化 GPU 資源」的正確方法：

| 場景 | 解法 | 支援 GPU |
|------|------|---------|
| GPU0 有 25% 閒置，想跑小 job | Time-Slicing 或 MIG `1g.10gb` | 全部 / 僅 A100,H100 |
| 多推論任務共用一張 GPU | MPS 或 time-slicing | Volta+ |
| 多租戶需記憶體隔離 | MIG | 僅 A100,H100,A30 |
| DDP 大型訓練 | 整張 GPU（1 job per GPU）| 全部 |
| 跨 GPU SM 碎片整合 | ❌ 硬體不支援 | 無 |

---

## AI/HPC Infra Review

### 實作設計是否妥當？

| 面向 | 評估 | 說明 |
|------|------|------|
| CPU consumable resource 模型 | ✅ 正確 | `cons_tres` + `CR_Core` 是 HPC 業界標準做法 |
| GPU 整卡獨占預設 | ✅ 合理 | DDP 訓練場景下整卡獨占是正確的起點 |
| `TaskPlugin=task/none` | ⚠️ 可接受於模擬 | Kind 環境做驗證可以，**不能用於效能測試或多租戶** |
| GPU GRES `File=/dev/null` | ✅ Kind 環境唯一可行方案 | 排程邏輯可驗證，硬體部分需真實環境補齊 |
| 無 GPU sharing 機制 | ✅ DDP 場景下正確 | DDP 不應 time-slice；加 GPU sharing 反而造成干擾 |

**整體評語：** 以驗證排程控制流為目標，目前設計選擇是合理的。主要缺口在隔離層（`task/none`）和 Kind GPU 模擬的不完整性，這兩個缺口在設計文件中已有明確說明，不是未知風險。

### Kind 環境 vs 真實環境差距

| 項目 | Kind 環境 | 真實 GPU 叢集 |
|------|----------|-------------|
| GPU 裝置 | `/dev/null`（純排程帳本） | NVIDIA device plugin，實際 GPU 分配 |
| CPU 隔離 | 無（task/none） | task/cgroup + cpuset 強制 binding |
| GPU sharing | 不可用 | Time-Slicing / MIG / MPS（按需啟用）|
| 記憶體限制 | Slurm 記帳但不強制 | cgroup v2 memory.max 強制 OOM |
| NCCL / collective | CPU 模擬 | NVLink / RDMA InfiniBand |

### 若要部署真實環境，建議的改動順序

1. 換掉 `TaskPlugin=task/none` → `task/cgroup`，配合 `CgroupPlugin=cgroup/v2`
2. 部署 NVIDIA GPU Operator，device plugin 取代 `/dev/null` 模擬
3. 視 GPU 型號選 sharing 策略：A100/H100 → MIG；一般推論服務 → time-slicing
4. Slurm `gres.conf` 對齊實際 MIG partition，`File=` 指向真實 `/dev/nvidia*`
5. Gang Scheduling → 啟用 K8s 1.35 `GangScheduling` feature gate（已完成）

---

## 快速查詢表

| 問題 | 答案 |
|------|------|
| Job 能指定 CPU 數量？ | ✅ `--cpus-per-task`、`--ntasks` |
| Job 能指定 GPU 型號和數量？ | ✅ `--gres=gpu:a10:1` |
| 同一 CPU worker 能跑多個 job？ | ✅ 排程層（`CR_Core` bin-packing） |
| CPU 有實體隔離？ | ❌ `task/none`，無 binding/cgroup（Kind 限制） |
| 同一張 GPU 能讓多個 job 共用？ | ✅ 可透過 time-slicing 或 MIG（需真實 GPU + 額外設定） |
| GPU core 能跨多張 GPU 分割給同一 job？ | ❌ CUDA 硬體架構根本限制 |
| Kind 環境的 GPU GRES 是真實硬體？ | ❌ `File=/dev/null`，純排程模擬 |
