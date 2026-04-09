# HPC / AI Infra Expert Review

> 以 HPC 學者與 AI 基礎架構工程師角度對本專案進行評估。
> 評估時間：2026-04-04。評估對象：Phase 1–4 全部已完成的實作。
>
> 本文件目的是讓資工學生了解「真實 HPC 叢集與 AI 訓練基礎設施」還需要考慮哪些面向，
> 不是否定現有設計，而是提供改進方向的路線圖。

---

## 執行摘要

本專案在「學習性原型」層次完成度相當高：以最小化依賴（Kind + StatefulSet + Python operator）
實作了 Slurm 彈性叢集，並加上 Prometheus/Grafana 監控堆疊。但作為可承載真實 PyTorch DDP
工作負載的 AI Infra，目前存在 **七大類別的設計缺口**，分別涉及：
安全模型、儲存層、GPU 資源管理、網路效能、排程策略、故障恢復，以及可觀測性的完整性。

---

## 1. 安全模型：多處學術原型假設需要修正

### 1-A. `SlurmUser=root` — 高風險

```ini
SlurmUser=root   # slurm-static.yaml
```

Slurm 服務程序（slurmctld、slurmd）以 `root` 身份運行。這在學習環境可以接受，但在任何
多租戶或聯網環境下，只要一個 CVE 就能完全取得節點權限。

**正確做法：**
```ini
SlurmUser=slurm   # 創建專屬低權限使用者
```
搭配 `slurm` 使用者對 `/var/spool/slurmctld`、`/var/log/slurm` 等路徑的最小化 ACL。

---

### 1-B. JWT Token 十年有效期 — 嚴重

```bash
scontrol token username=root lifespan=315360000  # 10 年
```

slurmrestd 的 JWT 金鑰以 10 年有效期發行，且存在 K8s Secret（base64 編碼，非加密）。
攻擊者取得 Secret 後可永久操作 Slurm API。

**正確做法：**
1. 使用短效 token（最多 1 小時），由 operator sidecar 定期輪換
2. K8s Secret 配合 [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) 或 Vault 加密存放
3. 或切換為 mTLS（`rest_auth/local` + client certificate）

---

### 1-C. `SLURMRESTD_SECURITY=disable_user_check` — 繞過使用者驗證

```bash
SLURMRESTD_SECURITY=disable_user_check
```

此旗標完全停用 slurmrestd 的使用者合法性驗證，任意 HTTP 請求只要帶上 `X-SLURM-USER-NAME: root`
即可操作叢集。在內部叢集若有 NetworkPolicy 保護尚可接受，但不應進入生產設定。

---

### 1-D. munge.key 靜態共享

munge.key 在部署時一次性產生並以 K8s Secret 分發給所有節點。沒有輪換機制。
若 key 洩漏，所有節點間的 Munge 認證可被偽造。

**改進：** 定期輪換腳本 + 滾動重啟（與節點 drain 配合）。

---

## 2. 儲存層：NFS 不適合 AI 訓練的 I/O Pattern

### 2-A. NFS 是 DDP checkpoint 的性能瓶頸

Phase 3 使用 NFS 作為共享儲存（`slurm-shared-rwx`，20 Gi）。在真實訓練場景下：

| 工作負載 | 典型需求 | NFS 問題 |
|--------|---------|---------|
| PyTorch checkpoint（GPT-2 6.7B）| 每個 ckpt ~13 GB | NFS 頻寬通常 < 1 GB/s，checkpoint 需要 15+ 秒，阻塞訓練 |
| NCCL All-Reduce | 數十 GB/s 的 GPU-to-GPU 頻寬 | NFS 完全無關，但 Pod 間若走同一 NIC 則競爭 |
| TensorBoard log | 連續小檔案隨機寫入 | NFS latency 遠高於本地 SSD |

**正確做法（依需求）：**
- 輕量原型：hostPath 掛載本地 NVMe scratch
- 生產環境：Lustre（最常見 HPC 選擇）或 GPFS / [GlusterFS](https://github.com/gluster/glusterfs)
- 雲端：AWS FSx for Lustre / GCP Filestore (enterprise tier)

---

### 2-B. StateSaveLocation 在容器內 ephemeral storage

```ini
StateSaveLocation=/var/spool/slurmctld
```

slurmctld 的 job state（Job DB、node state、job queue）存在容器本地磁碟。
Pod 重啟 = **所有 job state 遺失**，運行中的 job 無法恢復。

**正確做法：**
```yaml
# 掛載 PVC 或 hostPath
volumeMounts:
  - name: slurmctld-state
    mountPath: /var/spool/slurmctld
```
搭配 `slurmdbd`（Slurm DB daemon）將歷史記錄持久化到 MySQL/MariaDB。

---

### 2-C. 沒有 slurmdbd — 沒有會計功能

真實叢集必備的 `slurmdbd` 提供：
- 完整 job accounting（每個 job 用了多少 CPU-hours、GPU-hours）
- 使用者/帳號用量追蹤（Fairshare 計算依賴此資料）
- 歷史查詢（`sacct`）
- 多叢集聯邦（Federation）的基礎

沒有 slurmdbd，Phase 5 規劃的 Fairshare 功能從根本上無法實現。

**改進：** 部署 MySQL StatefulSet + slurmdbd sidecar，`slurm.conf` 加入：
```ini
AccountingStorageType=accounting_storage/slurmdbd
AccountingStorageHost=slurmdbd.slurm.svc.cluster.local
```

---

## 3. GPU 資源管理：目前是模擬，不是真實 GPU 排程

### 3-A. gres.conf 指向 `/dev/null`

```ini
# gres.conf
NodeName=slurm-worker-gpu-a10-0 Name=gpu Type=a10 File=/dev/null
```

GPU 宣告指向 `/dev/null`，代表 Slurm 雖然知道節點「有 GPU」，
但實際上不會分配任何設備給 job，也無法量測 GPU 使用率。

**真實環境需要：**
1. K8s 部署 [NVIDIA GPU Operator](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/overview.html)，讓 K8s 識別 GPU 設備
2. worker pod 的 `resources.limits` 宣告 `nvidia.com/gpu: 1`
3. gres.conf 的 `File` 指向真實設備（`/dev/nvidia0` 等）
4. 部署 [DCGM Exporter](https://github.com/NVIDIA/dcgm-exporter) 收集 GPU 利用率、溫度、顯存佔用、NVLink 流量等指標

### 3-B. 缺乏 MIG（Multi-Instance GPU）支援

A100/H100 支援 MIG 切割（如 1g.10gb, 2g.20gb），允許一張 GPU 同時服務多個小型工作負載。
目前架構無法表達 MIG profile，gres.conf 也沒有對應的 `Type` 設計。

---

### 3-C. SelectTypeParameters 只有 CR_Core，不考慮 GPU topology

```ini
SelectTypeParameters=CR_Core
```

這只做 CPU 核心層級的資源分配。對 GPU 工作負載，更重要的是考慮：
- 同一 NUMA domain 的 GPU 距離（NVLink vs PCIe）
- GPU 在相同 PCIe switch 下（避免跨 CPU socket 的 GPU-to-GPU 通訊）

正確設定應加入 `CR_Core_Memory` 並搭配 `GresTypes=gpu` + topology plugin。

---

## 4. 網路效能：DDP 的核心問題

### 4-A. MpiDefault=none — 無法跑任何 MPI/collective 工作負載

```ini
MpiDefault=none
```

設為 `none` 代表 Slurm 不為 job 初始化任何 MPI 環境。PyTorch DDP 使用 Gloo/NCCL 可以不透過 MPI，
但 OpenMPI/MVAPICH 工作負載（大量 HPC 應用）完全無法運行。

**改進：**
```ini
MpiDefault=pmix   # 現代 HPC 標準，支援 PMIx 初始化
# 或
MpiDefault=pmi2   # 向下相容
```

---

### 4-B. Phase 2-E 雙網卡設計的正確性問題

Phase 2-E 用 Multus CNI 加入第二個網卡（`net2`），設計意圖是將 DDP collective traffic 導向獨立子網。
但有幾個問題：

1. **沒有頻寬保證**：Kind 的 bridge CNI 沒有 QoS，兩個介面走同一個 veth/bridge，實際上沒有隔離
2. **沒有 RDMA**：真實 HPC 叢集用 InfiniBand 或 RoCE v2（RDMA over Converged Ethernet）達到 400 Gbps 低延遲互連；container 內部 veth 的延遲約 50–100 μs，IB 的 latency 約 1–2 μs，差距 50 倍以上
3. **NCCL socket path 未驗證**：`NCCL_SOCKET_IFNAME=net2` 設定是否真正讓 NCCL 用 net2 傳輸，沒有 nccl-tests 驗證

**真實 DDP 叢集的網路需求：**
- InfiniBand HDR（200 Gbps）或 RoCE v2 + SR-IOV
- K8s: [Mellanox Network Operator](https://github.com/Mellanox/network-operator) + SRIOV Device Plugin
- 或 AWS EFA（Elastic Fabric Adapter）搭配 `efa-device-plugin`

---

### 4-C. 節點間的 DDP 通訊路徑未最優化

Pod 的 IP 是 CNI 分配的虛擬 IP，NCCL 在 rendezvous 時會解析 hostname → Pod IP → 實際物理路徑。
在多節點 DDP（例如 8 節點 × 8 GPU = 64 GPU job）中，若拓樸感知（topology-aware scheduling）
沒有做好，跨 NUMA / 跨 rack 的 job placement 會讓 All-Reduce 帶寬降到最優解的 30–50%。

---

## 5. 排程策略：缺乏 HPC 標準功能

### 5-A. 只有一個 partition "debug"，沒有 QOS

所有節點在同一個 partition，`MaxTime=INFINITE`。真實叢集通常有：

```ini
PartitionName=interactive Nodes=cpu-[0-3] MaxTime=4:00:00 Priority=50
PartitionName=gpu-normal  Nodes=gpu-a10-[0-3] MaxTime=24:00:00 Priority=100 QOS=gpu-normal
PartitionName=gpu-preempt Nodes=gpu-h100-[0-3] MaxTime=INFINITE PreemptMode=REQUEUE
```

沒有 `MaxTime` 限制代表一個壞掉的 job 可以永遠佔用節點。

---

### 5-B. 沒有 Preemption 設定

若高優先度的緊急工作負載（例如 model serving 或緊急訓練）提交，無法搶佔已佔用資源的低優先度 job。

**改進：**
```ini
PreemptType=preempt/partition_prio
PreemptMode=REQUEUE   # 被搶佔的 job 重新排隊
# 或
PreemptMode=CHECKPOINT  # 被搶佔前先存 checkpoint
```

---

### 5-C. 沒有 Gang Scheduling（DDP 的 all-or-nothing 問題）

DDP job 需要「所有 rank 同時運行」。Slurm 原生的行為是：
只要申請的節點有空就開始跑，剩餘的 rank 等待。這在大型叢集（數百節點）會造成
**Backfill fragmentation**：部分 rank 已佔用 GPU，整個 job 仍無法開始，浪費資源。

真實 HPC 的解法：
- Slurm 的 `--exclusive` flag 搭配 job arrays
- 或使用 Volcano scheduler（K8s-native gang scheduling）

---

### 5-D. Backfill 參數未調整

```ini
SchedulerType=sched/backfill
```

Backfill scheduler 有幾個重要參數目前未配置（使用預設值）：

```ini
SchedulerParameters=bf_max_job_user=10,bf_max_job_test=500,bf_resolution=600
```

在 GPU 叢集中，若 `bf_resolution`（backfill 時間解析度）太大，scheduler 無法正確預測
短時間內的 GPU 釋放，導致 backfill 效果差。

---

## 6. 故障恢復：AI 訓練的核心需求

### 6-A. Scale-down 不做 Drain，直接殺 Pod

Operator 在縮容時直接調整 `replicas`，K8s 會按序號降序刪除 Pod（StatefulSet 行為）。
這意味著正在運行 job 的 worker 可能被強制殺掉，而 Slurm 不知情，controller 看到節點失聯
後才標記為 `DOWN`，此時 job 已經 FAILED。

**正確的縮容流程：**
```
1. 找出目標 Pod 上的 job → scontrol show job
2. 對節點執行 scontrol update node=X state=DRAIN reason="scale-down"
3. 等待節點上所有 job 完成（或 checkpoint + REQUEUE）
4. 再降低 replicas
```

目前 checkpoint guard 只檢查 checkpoint 檔案新鮮度，不是完整的 drain-then-scale 流程。

---

### 6-B. Controller 是 SPOF（Single Point of Failure）

slurmctld 跑在單一 `StatefulSet replicas: 1`，沒有 Hot Standby。
Pod crash 期間所有 job 提交暫停，正在 RUNNING 的 job 若 step launch 失敗也會中斷。

Slurm 原生支援 Hot Standby Controller：
```ini
SlurmctldHost=slurm-controller-0(...)
SlurmctldHost=slurm-controller-1(...)  # backup
```
在 K8s 上實現需要 shared PVC（兩個 controller 共享 `StateSaveLocation`）。

---

### 6-C. Operator 的 Cooldown 狀態不持久

```python
# phase2/operator/main.py
# Cooldown 存在記憶體 dict 中，Pod 重啟後 reset
```

若 operator Pod crash 並重啟，所有 pool 的 cooldown timer 歸零，可能立即觸發不必要的
scale-down（因為短暫認為 idle），造成運行中的 job 被中斷。

**改進：** 用 K8s Annotation 或 ConfigMap 持久化 cooldown timestamp。

---

## 7. 可觀測性：目前監控的盲點

### 7-A. 沒有 GPU 層級的指標

目前 Phase 4 的指標完全是 Slurm 層級（job queue、node state）。
缺少的 GPU 層級指標：

| 指標 | 工具 | 重要性 |
|------|------|-------|
| `DCGM_FI_DEV_GPU_UTIL` | DCGM Exporter | 最基本的 GPU 使用率 |
| `DCGM_FI_DEV_FB_USED` / `FB_FREE` | DCGM Exporter | 顯存使用情況，OOM 前兆 |
| `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` | DCGM Exporter | NVLink 通訊效率 |
| `DCGM_FI_DEV_SM_CLOCK` | DCGM Exporter | GPU 頻率降頻（thermal throttling）警告 |
| `DCGM_FI_DEV_POWER_USAGE` | DCGM Exporter | 功耗監控（資料中心成本） |

沒有這些指標，無法判斷 job 是否真正在 GPU 上高效運行（MFU, Model FLOP Utilization）。

---

### 7-B. 沒有 Job-level 資源使用率追蹤

目前只知道「幾個 job 在跑」，不知道每個 job 實際用了多少資源。
`sacct` 需要 `slurmdbd` 才能記錄完整的 per-job CPU/GPU/memory 使用統計。

---

### 7-C. 網路層指標缺失

AI 訓練效能的瓶頸通常在通訊而非計算：

- NCCL All-Reduce 吞吐量（GB/s）
- Job 內 rank-to-rank latency 分佈
- Pod 間 bandwidth（Prometheus `node_network_transmit_bytes_total` 有但沒進 dashboard）

---

### 7-D. Operator 的 Prometheus 指標缺少 job-level label

目前的 `_SCALE_UP_TOTAL`, `_CURRENT_REPLICAS` 等指標只有 `pool` label。
若要做 per-user 或 per-account 的 chargeback 分析，需要 job submission 相關的 label：

```python
# 建議加入的 label 維度
_JOB_WAIT_TIME = Histogram("slurm_job_wait_seconds", ...,
    labelnames=["partition", "account", "user", "job_type"])
```

---

## 8. Kubernetes 整合：幾個設計選擇的代價

### 8-A. ~~Operator 使用 subprocess 調用 kubectl CLI~~ ✅ 已修正（2026-04-09）

原本的問題：
```python
# 舊實作
result = subprocess.run(["kubectl", *args], ...)
```
每次 kubectl 調用是一個 fork + exec，每個 API call 約 50–200 ms overhead，
且 stdout/stderr parsing 脆弱（格式變化 silent fail）。

**已修正：** 改用 [kubernetes Python client](https://github.com/kubernetes-client/python)（`kubernetes==30.1.0`）
```python
# 新實作（phase2/operator/main.py）
from kubernetes import client as k8s_client, config as k8s_config
from kubernetes.stream import stream as k8s_stream

class K8sClient:
    def __init__(self, cfg):
        k8s_config.load_incluster_config()   # 自動取得 in-cluster ServiceAccount token
        self._core = k8s_client.CoreV1Api()
        self._apps = k8s_client.AppsV1Api()

    def patch_replicas(self, statefulset, replicas):
        self._apps.patch_namespaced_stateful_set(statefulset, namespace, {"spec": {"replicas": replicas}})
```
- Dockerfile 同步移除 kubectl binary 下載，映像縮小約 50 MB
- API 呼叫改為 in-process HTTPS，延遲降到 < 5 ms
- `exec_in_controller()` 改用 `kubernetes.stream` 取代 subprocess exec

---

### 8-B. 沒有 PodDisruptionBudget（PDB）

縮容時 K8s 可以同時刪除多個 Pod（若 replicas 變化 > 1）。
若沒有 PDB，可能導致叢集瞬間失去超過一半的 worker，破壞運行中的 DDP job。

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: slurm-worker-cpu-pdb
spec:
  maxUnavailable: 1   # 縮容時每次最多刪一個
  selector:
    matchLabels:
      app: slurm-worker-cpu
```

---

### 8-C. 沒有 ResourceQuota 與 LimitRange

所有 worker Pod 沒有 `resources.requests`/`limits` 宣告。
K8s scheduler 把它們視為 BestEffort QoS，在節點記憶體壓力時最優先被 OOM-killed。

---

## 9. 設計哲學的根本性選擇

### 9-A. Static pre-declared nodes 是一把雙刃劍

現有設計的核心決定：「所有節點在 `slurm.conf` 中靜態宣告，Operator 只調整 replicas」。

**優點（已充分利用）：**
- 避免 DNS 解析風暴
- Slurm 不需要動態 reconfigure
- 設計簡潔，易於理解

**代價（尚未面對）：**
- maxNodes 是硬上限，無法動態擴展池的上限（需要重新 render + apply slurm-static.yaml）
- 所有宣告節點的 FQDN 一直在 slurm.conf 中，即使節點不存在，造成 controller log 持續出現解析錯誤
- 節點規格（CPUs=4, RealMemory=3500）在 `slurm.conf` 中是靜態的，
  若換用不同規格的 K8s 節點，Slurm 看到的資源量和實際可用量不符

---

### 9-B. 沒有 CRD 的代價：運維能見度差

純 Python operator 不使用 CRD，帶來的問題：
- 無法用 `kubectl get slurm-pool` 看叢集狀態
- 無法用 K8s RBAC 限制哪些服務可以修改 pool 設定
- 缺少 Status subresource，無法透過 K8s API watch pool 的 conditions

Kopf 或 operator-sdk 雖然增加依賴，但提供了更完整的 reconciliation loop、
status reporting、event recording。

---

## 10. 與業界標準方案的比較

| 面向 | 本專案 | Volcano (K8s) | Open OnDemand + Slurm | AWS ParallelCluster |
|------|-------|--------------|----------------------|-------------------|
| Gang Scheduling | ✗ | ✓（原生） | ✗ | ✓（placement groups） |
| GPU 資源感知 | ✗（/dev/null） | ✓ | ✓ | ✓（NVIDIA GDRCopy） |
| Fairshare | ✗（無 slurmdbd） | ✓（Priority plugin） | ✓ | ✓ |
| 節點 Drain before scale | ✗ | N/A | ✓ | ✓ |
| Job accounting | ✗ | ✓ | ✓ | ✓ |
| HA controller | ✗ | ✓ | ✓ | ✓ |
| 並行 filesystem | NFS | 依 StorageClass | Lustre / GPFS | FSx for Lustre |

本專案的**差異化優勢**（vs 上述方案）在於：
- 保留 Slurm 語義（`sbatch`、`squeue`、`scontrol`），現有 HPC 使用者 0 學習成本
- 彈性 operator 是純 Python，對學術研究者可快速 customize
- Phase 4 的 SLO alerting 是大多數學術 HPC 方案沒有的功能

---

## 改進優先順序建議

| 優先順序 | 項目 | 影響 | 難度 |
|---------|------|-----|------|
| P0 | 修正 SlurmUser=root → slurm user | 安全 | 低 |
| P0 | StateSaveLocation 掛 PVC | 資料持久性 | 低 |
| P0 | 縮容前做 drain（搭配 checkpoint guard）| AI job 保護 | 中 |
| P1 | 部署 slurmdbd + MySQL | Fairshare 前置 | 中 |
| P1 | resources.requests/limits on worker pods | K8s QoS | 低 |
| P1 | 加入 PodDisruptionBudget | 縮容安全 | 低 |
| P1 | JWT token 輪換機制 | 安全 | 中 |
| ~~P2~~ | ~~換 kubernetes Python SDK 取代 kubectl subprocess~~ | ~~效能~~ | ✅ 已完成 |
| P2 | 加入 MIG partition 支援的 gres.conf 設計 | GPU 利用率 | 高 |
| P2 | DCGM Exporter + GPU dashboard | 可觀測性 | 中 |
| P3 | Gang scheduling（Volcano 整合或 Slurm --exclusive） | DDP 效能 | 高 |
| P3 | Lustre / BeeGFS 替代 NFS | I/O 效能 | 高 |

---

*本文件記錄為學習用途的架構審查，供後續 Phase 5+ 設計參考。*
