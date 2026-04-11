# HPC / AI Infra 架構審查報告

> **評估對象：** Phase 1–5 全部實作（Kind + Slurm + Elastic Operator + NFS + Lmod）
> **評估時間：** 2026-04-04（第一輪）、2026-04-10（第二輪，含 Phase 5）
> **評估角度：** HPC 學者與 AI 基礎架構工程師
>
> 本文件目的是讓學習者了解「真實 HPC 叢集與 AI 訓練基礎設施」還需要考慮哪些面向。
> ✅ 代表已在某 Phase 解決，並附簡述解法。

---

## 執行摘要

本專案以最小化依賴（Kind + StatefulSet + Python Operator）實作了一套彈性 Slurm 叢集，
涵蓋多池自動縮放、Prometheus 監控、共享 NFS 儲存、Lmod 模組系統。
在學習性原型層次完成度高，但距離可承載真實 PyTorch DDP 工作負載的 AI Infra，
仍存在 **七大類別**的設計缺口：安全模型、儲存層、故障恢復、排程策略、GPU 管理、K8s 整合、可觀測性。

**Phase 1–6 已解決項目（11 項）：**

| ✅ 已解決 | 解法簡述 | Phase |
|---------|---------|-------|
| StateSaveLocation 無持久化 | 掛 PVC 到 `/var/spool/slurmctld` | P2 |
| 縮容直接殺 Pod | Operator 先 `scontrol drain` 再降 replicas | P2 |
| 無 Job Accounting | 部署 slurmdbd + MySQL StatefulSet | P2 |
| kubectl subprocess 效能差 | 改用 kubernetes Python SDK（in-process HTTPS）| P2 |
| 無 PodDisruptionBudget | 為 7 個工作負載加入 `policy/v1` PDB | P5 |
| MpiDefault=none 無法跑 MPI | 改為 `MpiDefault=pmi2`，worker 加入 openmpi-bin | P5 |
| 無 HPC 模組系統 | 部署 Lmod + ConfigMap modulefiles（openmpi/python3/cuda）| P5 |
| CHECKPOINT_PATH="" 靜默失效 | 空路徑視為 guard 停用 + 啟動 WARN 日誌 | P5 |
| Worker preStop Hook 缺失 | lifecycle.preStop drain on K8s eviction | P5 |
| Job output 在 worker 本地 FS | 輸出路徑改為 `/shared/jobs/` NFS | P5 |
| NetworkPolicy 缺少 Egress | default-deny-egress + 各 Pod 類型最小化 Egress 白名單 | P6 |
| Operator 無熔斷器與就緒探針 | 指數退避 circuit breaker + `/tmp/operator-ready` readinessProbe | P6 |

---

## 一、安全模型

### ✅ 已解決：無（安全類均為待改進）

---

### 1-A. `SlurmUser=root` — 高風險

slurmctld、slurmd 以 `root` 身份運行。在任何多租戶或聯網環境下，
一個 CVE 即可取得節點完整控制權。

**改進方向：**
```ini
SlurmUser=slurm   # 建立低權限 slurm 系統帳號
```
搭配 `/var/spool/slurmctld`、`/var/log/slurm` 的最小化 ACL。

---

### 1-B. JWT Token 10 年效期

```bash
scontrol token username=slurm lifespan=315360000  # 315,360,000 秒 = 10 年
```

slurmrestd JWT 金鑰長期有效，且存於 K8s Secret（僅 base64 編碼，非加密）。
攻擊者取得 Secret 後可永久操作 Slurm REST API。

**改進方向：** lifespan 改為 `86400`（1 天），搭配 CronJob 定期輪換並更新 K8s Secret。

---

### 1-C. `SLURMRESTD_SECURITY=disable_user_check`

此旗標完全停用 slurmrestd 的使用者合法性驗證，任意 HTTP 請求只要帶上
`X-SLURM-USER-NAME: root` 即可操作叢集。應搭配 NetworkPolicy 嚴格限制訪問。

---

### 1-D. munge.key 靜態共享，無輪換機制

munge.key 一次性產生並以 K8s Secret 分發給所有節點。若 key 洩漏，
所有節點間的 Munge 認證可被偽造。

**改進方向：** 建立輪換腳本，配合節點 drain 後滾動重啟更新 key。

---

### ✅ 1-E. NetworkPolicy Egress 規則 — 已修正（Phase 6）

Phase 2-E 的 NetworkPolicy 只定義了 Ingress 規則，Pod 可對外任意發起連線：
- worker pod → 外部網路（資料洩漏風險）
- operator → 任意 K8s namespace（越權存取）

**解法（`phase2/manifests/network-policy.yaml`）：**
新增 `default-deny-egress`（`podSelector: {}` 拒絕所有出站流量），再針對每種 Pod 白名單最小必要 Egress：

| 新增 Policy | 保護的 Pod | 允許出站目標 |
|------------|-----------|------------|
| `default-deny-egress` | 全部 | 預設拒絕所有 egress |
| `allow-dns-egress` | 全部 | kube-dns UDP/TCP 53 |
| `allow-operator-egress` | operator | K8s API (TCP 443) + controller slurmrestd (TCP 6820) |
| `allow-controller-egress` | controller | workers (TCP 6818/22) + login (TCP 22) + NFS (TCP 2049) |
| `allow-worker-egress` | workers | controller (TCP 6817/22) + inter-worker MPI（any port）+ login (TCP 22) + NFS (TCP 2049) |
| `allow-login-egress` | login | controller (TCP 6817/6820/22) + workers (TCP 6818/22) + NFS (TCP 2049) |

Workers 的 inter-worker MPI 規則允許所有 port（NCCL/Gloo 使用 ephemeral ports），但出站目標嚴格限制在 `slurm` namespace 內的 worker pods，不允許連到外部網路。

Pod 間通訊（slurmctld ↔ slurmd）仍為明文 TCP，生產環境應搭配 Istio/Linkerd 提供 mTLS。

---

## 二、儲存與資料持久性

### ✅ 已解決：StateSaveLocation 持久化、Job Accounting

| 項目 | 解法 |
|-----|-----|
| slurmctld state 在容器 ephemeral storage，Pod 重啟即遺失 | 掛 PVC（`slurm-state-pvc`）到 `/var/spool/slurmctld` |
| 無 job accounting / fairshare 功能 | 部署 slurmdbd + MySQL StatefulSet，`slurm.conf` 加入 `AccountingStorageType=slurmdbd` |

---

### 2-A. NFS 是 DDP Checkpoint 的 I/O 瓶頸

Phase 3 使用 NFS（`slurm-shared-rwx`，20 Gi）作為共享儲存。
真實 AI 訓練場景下 NFS 是嚴重瓶頸：

| 工作負載 | 需求 | NFS 問題 |
|--------|------|---------|
| PyTorch checkpoint（GPT-2 6.7B） | 每個 ckpt ~13 GB | NFS 頻寬 < 1 GB/s，阻塞訓練 15 秒以上 |
| TensorBoard log | 連續小檔案隨機寫入 | NFS latency 遠高於本地 SSD |

**改進方向：** 輕量原型用 hostPath；生產環境用 Lustre / GPFS；雲端用 FSx for Lustre。

---

### ✅ 2-B. Job 輸出在 Worker 本地 FS — 已修正（Phase 5）

原本 `#SBATCH --output /tmp/job-%j.out` 指向 worker 本地磁碟：
- 使用者在 login pod 執行 `cat /tmp/job.out` 時找不到檔案（job 跑在不同 worker）
- Worker pod 縮容後輸出檔案永久消失

**解法：**
1. `bootstrap-phase5.sh` render 時改為 `--with-lmod --with-shared-storage`，掛載 Phase 3 NFS 到所有 pod
2. `bootstrap-phase5.sh` 啟動後執行 `mkdir -p /shared/jobs` 確保目錄存在
3. `verify-phase5.sh` batch script 的輸出路徑改為 `/shared/jobs/phase5-verify-%j.{out,err}`
4. 輸出讀取改從 login pod 讀取（共享 NFS），移除 `job_worker_pod()` worker 發現邏輯

---

### 2-C. MySQL 單點故障，無備份機制

MySQL StatefulSet 只有 1 replica，PVC 損毀或 `kind delete cluster` 後，
所有 job accounting 歷史全部遺失。無任何備份機制。

**改進方向：** 最小成本方案——建立每日 CronJob 執行 `mysqldump`，輸出到另一個 PVC；
生產環境使用 Percona XtraDB Cluster Operator 或 Velero + CSI snapshot。

---

## 三、故障恢復與可靠性

### ✅ 已解決：縮容 Drain、PodDisruptionBudget

| 項目 | 解法 |
|-----|-----|
| Operator 縮容直接殺 Pod，running job 立即失敗 | 縮容前對目標節點執行 `scontrol update state=DRAIN`，等節點 idle 後再降 replicas |
| K8s 節點維護可能同時驅逐多個 Slurm worker | 加入 7 個 PDB：controller/slurmdbd/operator 用 `minAvailable:1`，worker/login 用 `maxUnavailable:1` |

---

### ✅ 3-A. Worker Pod preStop Hook（K8s 直接驅逐場景）— 已修正（Phase 5）

Operator 縮容走 drain 流程，但 K8s 直接驅逐（節點壓力、手動 `kubectl drain node`）
仍會讓 slurmd 直接收到 SIGTERM，slurmctld 要等 `SlurmdTimeout`（預設 120s）才知道節點下線，
期間 job 進入 `NODE_FAIL` 且不會被重排。

**解法：** `render-slurm-static.py` 的 worker 容器 spec 加入 `lifecycle.preStop`，所有 worker pool 的 `slurm-static.yaml` 已重新 render：

```yaml
lifecycle:
  preStop:
    exec:
      command:
        - /bin/sh
        - -c
        - >-
          scontrol update nodename=$(hostname) state=drain reason=k8s-eviction
          2>/dev/null || true; sleep 10
```

與 Phase 2 operator drain 分工：
- **Operator drain**：operator 縮容前主動 drain，等 job 完成後降 replicas
- **preStop drain**：處理 K8s 直接驅逐（非 operator 觸發）的場景，給 slurmctld 10 秒通知時間

---

### ✅ 3-B. Checkpoint Guard 兩個靜默失效情境 — 已修正（Phase 5）

**情境 A — `CHECKPOINT_PATH=""` 讓 guard 變成 no-op：**

manifest 設定了 `CHECKPOINT_GUARD_ENABLED=true` 但 `CHECKPOINT_PATH=""`。
原本 `os.path.exists("")` 永遠 False → `checkpoint_age = None` → guard 阻擋所有縮容。

**解法（`main.py` `CheckpointAwareQueuePolicy.evaluate`）：**
```python
if not partition_cfg.checkpoint_path:
    pass  # 路徑未設定 — 此 pool 的 guard 視為停用
```
啟動時若 `CHECKPOINT_GUARD_ENABLED=true` 但路徑為空，emit WARN 日誌提示。

**情境 B — Job 尚未寫出 checkpoint 時 scale-down 被永久阻擋：**

Guard 看到「檔案不存在 → 視為 stale → 拒絕縮容」，job 啟動初期（尚未寫出第一個 checkpoint）永遠被阻擋。

**解法（`main.py` + `slurm-phase2-operator.yaml`）：**
- 新增 `CHECKPOINT_GRACE_SECONDS=300`（manifest 預設值）
- `OperatorApp` 用 `_checkpoint_missing_since` dict 記錄每個 pool 第一次看到「檔案不存在」的時間
- 在 grace period 內（`missing_since_seconds < grace`）允許縮容；超過後才阻擋
- `PartitionConfig` 新增 `checkpoint_grace_seconds`，支援透過 `PARTITIONS_JSON` 對每個 pool 獨立設定

---

### 3-C. Controller 是單點故障（SPOF）

slurmctld 跑在 `replicas: 1`，無 Hot Standby。Pod crash 期間所有 job 提交暫停。

Slurm 原生支援 Backup Controller：
```ini
SlurmctldHost=slurm-controller-0(...)
SlurmctldHost=slurm-controller-1(...)   # backup
```
K8s 上需要兩個 controller 共享同一 PVC（`StateSaveLocation`）。

---

### 3-D. Operator Cooldown 狀態不持久

Cooldown timer 存在 Python process 記憶體中，Pod 重啟後歸零，
可能立即觸發不必要的 scale-down，中斷運行中的 job。

**改進方向：** 用 K8s Annotation 或 ConfigMap 持久化每個 pool 的 last-scale timestamp。

---

## 四、排程策略

### ✅ 已解決：無

---

### 4-A. 只有一個 `debug` Partition，無 QoS，`MaxTime=INFINITE`

真實 HPC 叢集會依用途切分 partition，並設定時間上限防止殭屍 job 永占資源。

**改進方向：**
```ini
PartitionName=interactive  Nodes=cpu-[0-3]      MaxTime=4:00:00   Priority=50
PartitionName=gpu-normal   Nodes=gpu-a10-[0-3]  MaxTime=24:00:00  QOS=gpu-normal
PartitionName=gpu-preempt  Nodes=gpu-h100-[0-3] MaxTime=INFINITE  PreemptMode=REQUEUE
```

---

### 4-B. 無 Fairshare / Priority 設定

`slurmdbd` 已部署，但 `slurm.conf` 未啟用 multifactor priority：
```ini
PriorityType=priority/multifactor
PriorityWeightFairshare=100000
PriorityWeightAge=1000
PriorityDecayHalfLife=1-0
```
目前多個 DDP job 同時提交時，完全先進先贏，無公平分配。

---

### 4-C. 無 Preemption 設定

高優先度緊急工作負載無法搶佔已佔用資源的低優先 job。
```ini
PreemptType=preempt/qos
PreemptMode=REQUEUE   # 被搶佔 job 重新排隊
```

---

### 4-D. 無 Gang Scheduling（DDP all-or-nothing 問題）

DDP job 需要所有 rank 同時運行。Slurm 原生行為是部分 rank 先佔 GPU、其他等待，
造成 backfill fragmentation：GPU 被佔用但 job 無法啟動。

#### 問題根源

本架構存在兩層 gang scheduling 需求：

| 層次 | 問題 | 後果 |
|------|------|------|
| **K8s 層** | Operator scale up N replicas，K8s 可能只排程 M < N 個 Pod（資源不足） | Slurm 以為節點存在，收到 SlurmdTimeout 才知道節點下線 |
| **Slurm 層** | Slurm `backfill` 可能讓部分 rank 先佔資源 | `srun` step 無法啟動，DDP 進入死鎖等待 |

#### K8s 1.35 原生 Gang Scheduling（Alpha）

Kubernetes 1.35 引入 **Workload API**（`scheduling.k8s.io/v1alpha1`），第一個原生 gang scheduling 方案，需啟用 `GenericWorkload` 與 `GangScheduling` feature gate：

```yaml
# 1. 建立 Workload（定義 gang 條件）
apiVersion: scheduling.k8s.io/v1alpha1
kind: Workload
metadata:
  name: ddp-job-42
  namespace: slurm
spec:
  controllerRef:
    apiGroup: apps
    kind: StatefulSet
    name: slurm-worker-gpu-h100
  podGroups:
  - name: workers
    policy:
      gang:
        minCount: 4  # 必須 4 個 Pod 同時可排程，否則全部等待
```

```yaml
# 2. Pod template 加入 workloadRef
spec:
  workloadRef:
    name: ddp-job-42
    podGroup: workers
```

**行為：** Pod 進入 PreEnqueue 等待 minCount 達到 → WaitOnPermit gate 嘗試同時找位置 → 5 分鐘 timeout 後若無法全部排程則退回 unschedulable queue。

> **⚠️ 注意：** K8s 1.35 Gang Scheduling 為 Alpha，預設關閉。Kind 預設使用 K8s 1.29-1.31，需升級 Kind node image 並在 kube-scheduler 啟用 feature gate。

#### 舊有方案：kubernetes-sigs/scheduler-plugins Coscheduling（穩定，適合現有 Kind）

scheduler-plugins 的 `Coscheduling` 插件使用 `PodGroup` CRD，相比 K8s 1.35 Workload API 更成熟：

```yaml
# PodGroup CRD（scheduler-plugins v0.29+）
apiVersion: scheduling.sigs.k8s.io/v1alpha1
kind: PodGroup
metadata:
  name: ddp-job-42
  namespace: slurm
spec:
  minMember: 4
  minResources:
    cpu: "16"
    memory: "32Gi"
  scheduleTimeoutSeconds: 300
```

```yaml
# Pod annotation
metadata:
  labels:
    scheduling.sigs.k8s.io/pod-group: ddp-job-42
```

#### 在本架構的整合策略

Operator 在執行 scale_up 時，在 patch StatefulSet replicas 之前先建立 PodGroup / Workload：

```python
# phase2/operator/main.py — scale_up 路徑
def _create_pod_group(self, name: str, namespace: str, min_member: int) -> None:
    """建立 PodGroup 確保 all-or-nothing 排程。"""
    body = {
        "apiVersion": "scheduling.sigs.k8s.io/v1alpha1",
        "kind": "PodGroup",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {"minMember": min_member, "scheduleTimeoutSeconds": 300},
    }
    self.client._custom.create_namespaced_custom_object(
        "scheduling.sigs.k8s.io", "v1alpha1", namespace, "podgroups", body
    )
```

#### Slurm 層的補充措施

K8s gang scheduling 確保「Pod 全部上線」，但 Slurm backfill 仍可能讓部分 rank 先佔 slot。補充方案：

```bash
# 提交時加 --exclusive 確保節點獨佔，避免 backfill fragmentation
#SBATCH --exclusive
#SBATCH --ntasks=4
#SBATCH --gres=gpu:h100:1

# 或搭配 --wait-all-nodes=1（等所有節點都 ready 才啟動 step）
#SBATCH --wait-all-nodes=1
```

#### Kind vs 真實環境實作對照

| 面向 | Kind Lab 建議 | 真實環境建議 |
|------|-------------|------------|
| Gang Scheduling 機制 | scheduler-plugins Coscheduling（PodGroup，穩定）| K8s 1.35 Workload API（Alpha）或 Volcano |
| Feature gate | 不需要（scheduler-plugins 獨立插件）| `--feature-gates=GenericWorkload=true,GangScheduling=true` |
| Operator 整合 | 新增 `_create_pod_group()` 於 scale_up 路徑 | 同左，或改用 Volcano `Job` CRD 取代 Slurm job 提交 |
| Slurm 層補充 | `--exclusive` + `--wait-all-nodes=1` | 同左 + `OverSubscribe=NO` partition 設定 |
| Timeout 處理 | PodGroup scheduleTimeoutSeconds=300 | 同左，搭配 Prometheus alert on pending PodGroup |

**改進方向：** 短期用 `--exclusive` + Slurm `--wait-all-nodes=1` 解決 Slurm 層；中期部署 scheduler-plugins Coscheduling + Operator 整合 PodGroup 建立；長期升級至 K8s 1.35+ 使用原生 Workload API。

---

## 五、GPU 管理與 HPC 功能

### ✅ 已解決：MpiDefault pmi2、Lmod 模組系統

| 項目 | 解法 |
|-----|-----|
| `MpiDefault=none` 無法跑任何 MPI/collective 工作負載 | 改為 `MpiDefault=pmi2`；worker image 加入 `openmpi-bin + libopenmpi-dev` |
| 無 HPC 模組管理（`module load`） | 部署 Lmod；ConfigMap 提供 openmpi/4.1、python3/3.10、cuda/stub 模組；`/etc/lmod/modulespath` 讓 sbatch 非登入 shell 也可用 |

---

### 5-A. gres.conf 指向 `/dev/null`，GPU 排程是假的

```ini
NodeName=slurm-worker-gpu-a10-0 Name=gpu Type=a10 File=/dev/null
```

Slurm 雖知節點「有 GPU」，但不會分配任何設備給 job，也無法量測 GPU 使用率。

**真實環境需要：**
1. K8s 部署 NVIDIA GPU Operator
2. worker pod `resources.limits: nvidia.com/gpu: 1`
3. gres.conf `File=/dev/nvidia0`
4. DCGM Exporter 收集 GPU 利用率、顯存、NVLink 流量

---

### 5-B. 缺乏 MIG（Multi-Instance GPU）支援

A100/H100 支援 MIG 切割（1g.10gb, 2g.20gb），允許一張 GPU 同時服務多個小型工作負載。
目前架構無法表達 MIG profile，gres.conf 也沒有對應 `Type` 設計。

---

### 5-C. Lmod 模組設計缺口

- **缺 `conflict` / `prereq` 宣告**：使用者可同時載入互斥的 MPI 實作，導致 `LD_LIBRARY_PATH` 混亂
  ```lua
  conflict("openmpi")   -- 同名模組只能載一個
  conflict("mvapich2")  -- 互斥 MPI 實作
  ```
- **缺 NCCL 模組**：PyTorch DDP 主要用 NCCL backend，目前無 `nccl/2.x.lua`
- **ConfigMap 大小限制**：單一 key 上限 1 MiB；完整 CUDA toolkit 可能超限，需改用 PVC + initContainer

---

### 5-D. DDP 網路效能問題

Phase 2-E 用 Multus CNI 加入第二網卡，但 Kind bridge CNI 無 QoS，兩個介面實際共享同一 veth/bridge，無真正隔離。

真實 HPC 需要：
- InfiniBand HDR（200 Gbps）或 RoCE v2 + SR-IOV
- NCCL socket path 驗證（目前 `NCCL_SOCKET_IFNAME=net2` 設定未以 nccl-tests 驗證）

---

## 六、Kubernetes 整合

### ✅ 已解決：kubectl subprocess → Python SDK、PodDisruptionBudget

| 項目 | 解法 |
|-----|-----|
| Operator 用 subprocess 呼叫 kubectl CLI（每次 50–200ms overhead，解析脆弱） | 改用 `kubernetes==30.1.0` Python SDK；`k8s_config.load_incluster_config()` 取 ServiceAccount token；exec 改用 `kubernetes.stream` |

---

### 6-A. 所有 Pod 為 BestEffort QoS，OOM 時最先被殺

所有 Pod 均未設定 `resources.requests/limits`，K8s 節點記憶體壓力時
slurmctld 或 worker pod 會被優先驅逐，running job 直接失敗。

**改進方向：**
```yaml
resources:
  requests:
    cpu: "500m"
    memory: "512Mi"
  limits:
    cpu: "2"
    memory: "2Gi"
```
worker pod 建議 `requests == limits`（Guaranteed QoS）。GPU worker 需加 `nvidia.com/gpu: 1`。

---

### ✅ 6-B. Operator 熔斷器與就緒探針 — 已修正（Phase 6）

- **無熔斷器**：K8s API 短暫不可用時，while-loop 持續重試產生大量 error log，可能誤觸 rate-limit
- **無 readinessProbe**：Pod 重啟後立即視為 Ready，但 operator 可能尚在初始化 pool state

**解法（`phase2/operator/main.py` + `phase2/manifests/slurm-phase2-operator.yaml`）：**

**熔斷器（circuit breaker）：**
- `OperatorApp.__init__` 新增 `_consecutive_errors: int = 0`
- `_CIRCUIT_BREAKER_ERRORS` Gauge（Prometheus metric）追蹤連續失敗次數
- `collect_all_partition_states()` 呼叫被 try-except 包覆：失敗時 `_consecutive_errors += 1`，睡眠 `min(2^consecutive, 60)` 秒（最大 60s，低於 liveness 120s 閾值），emit `error` event 後 `continue`
- 恢復正常後 emit `circuit_closed` event 並重置計數器
- 即使在 backoff 期間也更新 `/tmp/operator-alive`，避免 livenessProbe 誤殺 Pod

**就緒探針（readinessProbe）：**
- 每次成功完成完整 poll 迴圈後，寫入 `/tmp/operator-ready`（透過 `pathlib.Path.touch()`）
- `slurm-phase2-operator.yaml` 加入：
  ```yaml
  readinessProbe:
    exec:
      command: [/bin/sh, -c, "test -f /tmp/operator-ready"]
    initialDelaySeconds: 30
    periodSeconds: 10
    failureThreshold: 3
  ```
  Pod 在第一次成功 poll（初始化完成）前不會被路由流量，避免 operator 尚未完整載入 pool state 時接受 K8s 事件。

---

### 6-C. Static Pre-declared Nodes 的設計代價

優點（已充分利用）：無 DNS 解析風暴、Slurm 不需動態 reconfigure。

代價：
- `maxNodes` 是硬上限，擴展需重新 render + apply `slurm-static.yaml`
- 未啟動的節點 FQDN 一直在 `slurm.conf`，controller log 持續出現解析警告
- CPU/Memory 規格靜態宣告，換 K8s 節點規格時 Slurm 看到的資源量與實際不符

---

### 6-D. 無 CRD 的運維能見度缺口

純 Python operator 不使用 CRD：
- 無法 `kubectl get slurm-pool` 看池狀態
- 無法用 K8s RBAC 限制誰可修改 pool 設定
- 缺少 Status subresource，無法 watch pool conditions

Kopf 或 operator-sdk 提供更完整的 reconciliation loop、status reporting、event recording。

---

## 七、可觀測性

### ✅ 已解決：Prometheus + Grafana 基礎監控（Phase 4）

Phase 4 部署了 slurm-exporter，提供 job queue 長度、node state、scaling 事件等 Slurm 層級指標。

---

### 7-A. 缺少 GPU 層級指標

| 指標 | 工具 | 重要性 |
|------|------|-------|
| `DCGM_FI_DEV_GPU_UTIL` | DCGM Exporter | GPU 使用率 |
| `DCGM_FI_DEV_FB_USED/FREE` | DCGM Exporter | 顯存（OOM 前兆） |
| `DCGM_FI_DEV_NVLINK_BANDWIDTH` | DCGM Exporter | NVLink 通訊效率 |
| `DCGM_FI_DEV_SM_CLOCK` | DCGM Exporter | thermal throttling 警告 |

沒有這些指標，無法判斷 job 是否真正在 GPU 上高效運行（MFU）。

---

### 7-B. 缺少 Per-Job 資源用量追蹤

目前只知道「幾個 job 在跑」，不知道每個 job 實際用了多少 CPU/GPU/memory。
`sacct` 已可查歷史記錄（slurmdbd 已部署），但 Grafana dashboard 未整合 per-job label：

```python
_JOB_WAIT_TIME = Histogram("slurm_job_wait_seconds", ...,
    labelnames=["partition", "account", "user"])
```

---

## 業界比較

| 面向 | 本專案（Phase 5） | Volcano (K8s) | Open OnDemand + Slurm | AWS ParallelCluster |
|------|:--------------:|:------------:|:--------------------:|:-----------------:|
| Gang Scheduling | ✗ | ✓（原生） | ✗ | ✓（placement groups） |
| GPU 資源感知 | ✗（/dev/null） | ✓ | ✓ | ✓（GDRCopy） |
| Fairshare / Priority | ✓（slurmdbd 已部署，未啟用） | ✓ | ✓ | ✓ |
| 縮容前 Drain | ✓（Phase 2） | N/A | ✓ | ✓ |
| Job Accounting | ✓（slurmdbd + MySQL） | ✓ | ✓ | ✓ |
| HA Controller | ✗ | ✓ | ✓ | ✓ |
| 共享 Filesystem | NFS（Phase 3） | 依 StorageClass | Lustre / GPFS | FSx for Lustre |
| HPC Module 系統 | ✓（Lmod，Phase 5） | ✗ | ✓（Environment Modules） | ✓（Lmod） |
| PodDisruptionBudget | ✓（Phase 5） | N/A | N/A | N/A |
| 資源 QoS 配置 | ✗（BestEffort） | ✓ | ✓ | ✓ |
| Slurm 語義相容 | ✓（0 學習成本） | ✗（需學 Volcano API） | ✓ | ✓ |

**本專案差異化優勢：** 保留 Slurm 語義（`sbatch / squeue / scontrol`）、Operator 純 Python 易 customize、Prometheus SLO alerting 是多數學術方案所缺。

---

## 改進優先順序總表

| 優先 | 項目 | 類別 | 難度 |
|:---:|------|-----|:---:|
| **P0** | 修正 `SlurmUser=root` | 安全 | 低 |
| **P1** | 所有 Pod 加入 resources.requests/limits | K8s 整合 | 低 |
| **P1** | JWT Token 輪換機制（lifespan → 1 天） | 安全 | 中 |
| **P2** | slurm.conf QoS + Preemption + MaxTime | 排程 | 中 |
| **P2** | Fairshare (multifactor priority) 設定 | 排程 | 中 |
| **P2** | MySQL CronJob 備份 | 儲存 | 低 |
| **P2** | Lmod conflict/prereq + NCCL 模組 | GPU / HPC | 中 |
| **P2** | DCGM Exporter + GPU Grafana dashboard | 可觀測性 | 中 |
| **P2** | gres.conf 真實 GPU 設備（需 NVIDIA GPU Operator）| GPU / HPC | 高 |
| **P3** | Checkpoint Grace Period 設計 | 故障恢復 | 中 |
| **P3** | Operator Cooldown 持久化（K8s Annotation）| K8s 整合 | 低 |
| **P3** | Gang Scheduling（Volcano 或 `--exclusive`）| 排程 | 高 |
| **P3** | Lustre / BeeGFS 替代 NFS | 儲存 | 高 |
| **P3** | HA Backup Controller | 故障恢復 | 高 |

---

*本文件為學習用途架構審查，供後續 Phase 6+ 設計參考。*
