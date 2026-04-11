# Development Notes

這份筆記保留原本的階段紀錄，以及這一輪開發實際踩到的坑。

---

# Phase 1

- 建立 Slurm Controller / Worker 映像。
- 在 Kind 部署靜態 Slurm 叢集。
-  讓 Pod 間具備 SSH 互通與 Munge 認證。

## Debug Record

### A. Secret volume 唯讀，不能直接 chmod

觀察到：
```
chmod: changing permissions of '/etc/munge/munge.key': Read-only file system
```
原因在於 K8s Secret mount 是唯讀。

修正方式：

- 改掛到 `/slurm-secrets/munge.key`。
- 啟動時複製到 `/etc/munge/munge.key` 後再 `chown/chmod`。

### B. `SlurmctldHost` / DNS 解析錯誤

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
- 讓整個 Phase 1 + Phase 2 能以 `bootstrap-dev.sh` / `verify-dev.sh` 穩定驗證。
- (2-B) 加入結構化日誌，讓 autoscaling 行為可追蹤、可分析、可做報告。
- (2-C) 把單一 worker pool 擴展成 multi-pool / partition-aware autoscaling。
- (2-D) 加入 checkpoint-aware scale-down guard，避免正在跑的工作因過早縮容而丟失恢復點。
- (2-E) 在單一叢集引入兩個子網路，分成 management subnet 和 data subnet。
  - Slurm 控制流量維持單純
  - 之後若要做 PyTorch DDP / MPI / NCCL，能逐步把高流量傳輸導向 `data subnet`
  - verify 時也能清楚展示哪些元件是 control plane、哪些元件是 dual-homed compute plane

## Debug Record

### A. `duplicate partition in config: debug`

觀察到 operator 啟動時直接 `ValueError: duplicate partition in config: debug`

原因是：原本 validation 把「partition 名稱重複」當成非法，但現在多 pool 共享同一個 Slurm partition 是設計需求，不是錯誤。

修正方法：

- validation 不能用 partition name 當唯一鍵。
- 要接受「同一 partition 對應多個 worker pool」。

## 目前可接受的系統現象

以下現象目前可以接受，不應直接視為功能失敗：

1. controller log 中偶爾出現對不存在 FQDN 的解析錯誤。
   - 因為 `slurm.conf` 有宣告 max node set。
   - 在動態 Pod 尚未存在時，controller 可能暫時解析不到。

2. `squeue` / `sinfo` 偶發 timeout。
   - 在 reconfigure 或 node registration 時可能發生。
   - verify 已盡量降低對這些瞬時抖動的敏感度。

3. GPU pool 預設為 0 replicas。
   - 只在有對應 constraint / gres 的工作時才拉起。
   - 驗證時這是預期行為。

## 目前通過的 acceptance path

### CPU path

- controller / login / baseline cpu worker 啟動成功
- `scontrol ping` 成功
- `srun` 成功
- `sbatch` CPU smoke job 成功
- CPU scale-up 成功
- CPU scale-down 成功

### GPU path

- GPU job 送出後，operator 會把 `slurm-worker-gpu-a10` 從 0 拉到 1
- job 可落到 `slurm-worker-gpu-a10-0`
- 工作完成後 pool 可縮回 0

## 目前限制

### 1. 這不是「開箱即用的真雙網」，而是以 repo 現況為基礎的最小可用落地版

因為你目前用的是 kind，原生不會自己給第二張 Pod NIC。真正要讓 runtime 出現雙網，還需要：

- Multus CNI
- `NetworkAttachmentDefinition`
- cluster node 內對應 bridge / IPAM 可正常工作

### 2. operator 目前還沒有用 topology 自動決定某個 job 要走哪張 NIC

也就是說，Phase 2-E 目前完成的是：

- 網路拓撲建模
- workload placement 規劃
- runtime 驗證 scaffolding

下一步若要更深入，才是：

- 將 `data interface` 寫入 worker 啟動流程
- 讓 DDP / MPI workload 明確選用 `net2`
- 將 checkpoint / shared storage traffic 與 control traffic 分離

# Job-Hardware Mapping

本節為 **AI/HPC Infra 視角的完整評審**，涵蓋 Slurm + K8s 雙層資源模型的分配語意、實際邊界、以及 Kind 模擬環境與真實 GPU 叢集的差距。

---

## 資源模型概覽

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

每台 GPU worker 宣告 `Gres=gpu:a10:1`（1 張）或 `Gres=gpu:h100:1`（1 張）。GRES 是整數消耗，無分數分配：

```
Job A: --gres=gpu:a10:1  →  佔用整張 A10，該 worker GRES=0
Job B: --gres=gpu:a10:1  →  必須等 A 結束，或排到另一台 gpu-a10-worker
```

Kind 環境下 GPU worker 宣告 `File=/dev/null`，排程帳本上是 1 個 GPU slot，但實際上不存在硬體。

### 分配範例

場景：2 台 A10 GPU worker + 3 個 GPU job

| Job | 請求 | 分配結果 |
|-----|------|---------|
| A `--gres=gpu:a10:1 -N 1` | 1 A10 | → worker-gpu-a10-0，整張 A10 獨占 |
| B `--gres=gpu:a10:1 -N 1` | 1 A10 | → worker-gpu-a10-1，整張 A10 獨占 |
| C `--gres=gpu:a10:1 -N 1` | 1 A10 | → Pending，等 A 或 B 釋放 |

多節點 DDP job：

```bash
#SBATCH --gres=gpu:h100:1
#SBATCH -N 4          # 需要 4 台 H100 worker
#SBATCH --ntasks-per-node=1
```

Slurm 要求同時有 4 台 `gpu-h100` worker 都空閒。這正是 Gang Scheduling 解決的問題——若只有 3 台空閒，K8s 1.35 原生 `GangScheduling` 會讓 4 個 worker Pod 要嘛全部調度，要嘛全不調度，避免佔著資源等人。

### GPU 共用機制（進階）

若要讓多個 job 共用同一張 GPU，需要額外機制：

#### Time-Slicing（時間切片）

CUDA context 輪流使用 GPU，類似 CPU 分時多工。

- 適用：**所有 NVIDIA GPU**
- 隔離：**無記憶體隔離**（所有 context 共享 VRAM），context switch 有開銷
- K8s 設定：GPU Operator ConfigMap 把 1 張 GPU 虛擬成 N 份 `nvidia.com/gpu`

```yaml
# ConfigMap：1 張 A10 虛擬成 4 份
sharing:
  timeSlicing:
    resources:
    - name: nvidia.com/gpu
      replicas: 4
```

```ini
# gres.conf（Slurm 端）
NodeName=slurm-worker-gpu-a10-0 Name=gpu Type=a10 Count=4 File=/dev/nvidia0
```

4 個 job 可同時各請求 `--gres=gpu:a10:1`，時間輪流使用同一張 GPU。**不適合 DDP 訓練**（延遲不可預測、無記憶體保護）。

#### MIG（Multi-Instance GPU）— 硬體分割

A100 / H100 / A30 支援在**硬體層**將 GPU 切成獨立 instance，各有專屬 SM、L2 cache、VRAM 帶寬，完全隔離。

| Profile | SM | VRAM | 每張 A100 80GB 可建 |
|---------|----|------|-------------------|
| `1g.10gb` | 1/7 GPU | 10 GB | 最多 7 個 |
| `2g.20gb` | 2/7 GPU | 20 GB | 最多 3 個 |
| `3g.40gb` | 3/7 GPU | 40 GB | 最多 2 個 |
| `7g.80gb` | 完整 GPU | 80 GB | 1 個（無分割）|

```ini
# gres.conf（MIG 模式）
NodeName=slurm-worker-gpu-h100-0 Name=gpu Type=mig-2g.20gb Count=3
NodeName=slurm-worker-gpu-h100-0 Name=gpu Type=mig-1g.10gb Count=1
```

Job 請求 `--gres=gpu:mig-2g.20gb:1`，排到任何有空閒 MIG instance 的 node。**僅限 A100/H100/A30；最細粒度 1/7 GPU，仍不能跨 GPU。**

#### MPS（Multi-Process Service）

多個 CUDA process 合併進同一 CUDA context，共享 command queue 和 SM，減少 context switch 開銷。SM 可設 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 比例。適合延遲敏感的小型推論，但無完整記憶體隔離。

### 核心限制：為何不能跨 GPU 分割 SM？

用戶場景：GPU0 剩 4 SM 閒置、GPU1 剩 4 SM 閒置，Job C 需要 8 SM，能否合用？

**直接答案：不可能。** 硬體架構根本限制：

```
GPU0 [SM0..SM107]           GPU1 [SM0..SM107]
┌──────────────────┐        ┌──────────────────┐
│  SM0  SM1  ...   │        │  SM0  SM1  ...   │
│  L2 Cache        │        │  L2 Cache        │
│  HBM (80 GB)     │<─PCIe/NVLink─>│  HBM (80 GB)     │
└──────────────────┘        └──────────────────┘
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

## AI/HPC Infra 專家評審

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

1. **換掉 `TaskPlugin=task/none`** → `task/cgroup`，配合 `CgroupPlugin=cgroup/v2`
2. **部署 NVIDIA GPU Operator**，device plugin 取代 `/dev/null` 模擬
3. **視 GPU 型號選 sharing 策略**：A100/H100 → MIG；一般推論服務 → time-slicing
4. **Slurm `gres.conf` 對齊實際 MIG partition**，`File=` 指向真實 `/dev/nvidia*`
5. **Gang Scheduling** → 啟用 K8s 1.35 `GangScheduling` feature gate（本 repo 已完成）

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

verify-dev.sh 的 smoke test 刻意使用 `sleep N` job，迴避了這個問題。

### Phase 3 實作現況

Phase 3 **已完成實作**，並非只有設計：

| 檔案 | 內容 |
|------|------|
| `phase3/scripts/setup-nfs-server.sh` | 在 Windows 11 主機上建立 NFS Server（`/srv/nfs/k8s`） |
| `phase3/manifests/nfs-subdir-provisioner.tmpl.yaml` | NFS subdir external provisioner Deployment（需替換 `__NFS_SERVER__` / `__NFS_PATH__`） |
| `phase3/manifests/shared-storage.yaml` | StorageClass `slurm-shared-nfs` + PVC `slurm-shared-rwx`（20Gi RWX） |
| `phase3/scripts/bootstrap-phase3.sh` | 部署 provisioner → 建立 PVC → patch controller/worker/login 加入 `/shared` mount |
| `phase3/scripts/verify-phase3.sh` | 驗證 PVC Bound + `/shared` 掛載在所有 pod 上 |
| `phase3/scripts/verify-phase3-e2e.sh` | **完整 e2e 測試**：login 提交 `sbatch -o /shared/out-%j.txt`，等待完成，從 login 讀回輸出驗證 |

Phase 3 部署後，`/shared` 以 ReadWriteMany 方式同時掛載到：
- `slurm-controller-0`
- `slurm-worker-0`（以及所有副本）
- `slurm-login`

使用者只需在 job script 加入：

```bash
#SBATCH --output=/shared/out-%j.txt
#SBATCH --error=/shared/err-%j.txt
```

job 完成後即可在 login pod 的 `/shared/` 直接讀取輸出。

### 部署順序建議

在 Phase 2 + Phase 3 同時啟用時，建議部署順序為：

```
1. bash scripts/bootstrap-dev.sh          # Phase 1 + Phase 2
2. sudo bash phase3/scripts/setup-nfs-server.sh  # 主機端 NFS（一次性）
3. NFS_SERVER=<ip> bash phase3/scripts/bootstrap-phase3.sh
4. bash phase3/scripts/verify-phase3.sh
5. WORKER_STS=slurm-worker-cpu bash phase3/scripts/verify-phase3-e2e.sh
```

---

# Phase 4 (DDP)

## 1. Worker Image 加入 PyTorch（DDP 前置條件）

目前 worker image 只有 Slurm + Munge，要跑 DDP 需要加入 PyTorch：

```dockerfile
# phase1/docker/worker/Dockerfile 加入
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

## 4. Prometheus + Grafana 監控

詳細規格見 `docs/monitoring.md`。核心是三層 metrics：

| 來源 | 取得方式 | 關鍵指標 |
|------|---------|---------|
| slurm-exporter | scrape slurmrestd（REST API） | queue_pending, nodes_idle |
| kube-state-metrics | K8s 原生 | StatefulSet replicas, Pod ready |
| operator 自定義 | prometheus_client HTTP server | scale_events, guard_blocks |

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

## 改進優先順序

```
Phase 4 必做（影響系統穩定性與 DDP 可用性）
  1. Slurm REST API 取代 kubectl exec
  2. Worker image 加 PyTorch
  3. 標準 DDP sbatch 腳本 + train.py
  4. SIGTERM handler 串接 checkpoint guard

Phase 4 主體（可觀測性 demo）
  5. Prometheus + Grafana（見 docs/monitoring.md）

可選加分項（提升系統完整度）
  6. Job 提交前健康檢查（Slonk 模式）
  7. Elastic DDP（torchrun min:max nodes）
  8. Soperator reconciliation 安全性參考
```

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

# Phase 4 Debug Record

## A. slurm-exporter 無法連線到 slurmrestd（NetworkPolicy 缺規則）

**症狀：**
```
urllib.error.URLError: <urlopen error timed out>
```
slurm-exporter 每次 scrape 都超時，所有 Slurm 指標歸零（`slurm_queue_pending=0`、`slurm_nodes_total=0`），但 `/metrics` endpoint 本身可正常被 Prometheus 抓到（`slurm_exporter_scrape_success=0`）。

**根因：**

`phase2/manifests/network-policy.yaml` 的 `allow-controller-ingress` policy 允許的 source pod 清單為：
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
kubectl apply -f phase2/manifests/network-policy.yaml
kubectl -n slurm rollout restart deployment/slurm-exporter
```

---

## B. verify-phase4.sh 在無 wget/curl 的 image 裡 exec 失敗

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

**另一個隱藏 bug：**

`check_metrics` 函數原本先把 curl 結果存入 bash 變數（`output=$(curl ...)`），再用 `echo "$output" | grep -q`。對於 KSM 的 ~180 KB metrics body，`echo "$output"` 在 bash pipe 中有截斷風險。改為直接 pipe `curl ... | grep -q` 後問題消失。


---

# Phase 5 技術規劃：平台化與高可用（2026-04-04）

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

## P0/P1 改進部署踩坑紀錄（2026-04-09）

### 問題 1：slurmdbd 映像缺少 slurmdbd 套件

**現象：** `slurmdbd` pod CrashLoopBackOff，log 顯示 `/bin/bash: line 23: exec: slurmdbd: not found`。

**原因：** `slurm-accounting.yaml` 使用 `slurm-controller:phase1` 映像跑 slurmdbd，但 controller Dockerfile 只安裝了 `slurmctld`、`slurmd`、`slurmrestd`，沒有裝 `slurmdbd` 套件（Ubuntu 22.04 的 `slurmdbd` 是獨立套件）。

**修法：** `phase1/docker/controller/Dockerfile` 加入 `slurmdbd`，重新 build + kind load。

---

### 問題 2：slurmdbd 啟動後 hostname 不符

**現象：** slurmdbd 啟動後立即 fatal exit：`This host not configured to run SlurmDBD (slurmdbd-xxx != slurmdbd)`。

**原因：** `slurmdbd.conf` 的 `DbdHost=slurmdbd`，但 Deployment pod 的 hostname 是 `slurmdbd-{replicaset}-{random}`（Kubernetes 預設行為）。slurmdbd 在啟動時會驗證 `DbdHost` 是否匹配當前 hostname。

**修法：** `slurm-accounting.yaml` 的 Deployment pod spec 加入 `hostname: slurmdbd`，讓 pod hostname 固定為 `slurmdbd`。

---

### 問題 3：slurmctld 首次啟動 fatal（TRES 缺失）

**現象：** 新叢集第一次啟動時，slurmctld fatal exit：`You are running with a database but for some reason we have no TRES from it`。

**原因：** `slurm.conf` 設定了 `AccountingStorageType=accounting_storage/slurmdbd`，slurmctld 啟動時需要從 slurmdbd 取得 TRES（Trackable RESources）定義。若 slurmdbd 尚未 ready（容器剛建立），且又沒有本地 state file，slurmctld 就會 fatal exit 而非等待。

**修法：** `render-slurm-static.py` 的 controller 啟動腳本加入 wait loop：偵測到 `AccountingStorageType=slurmdbd` 時，先用 bash TCP 連線確認 `slurmdbd.slurm.svc.cluster.local:6819` 可達，再 exec slurmctld。

```bash
if grep -q 'AccountingStorageType=accounting_storage/slurmdbd' /etc/slurm/slurm.conf; then
  until (echo >/dev/tcp/slurmdbd.slurm.svc.cluster.local/6819) 2>/dev/null; do sleep 3; done
fi
exec slurmctld -Dvvv
```

---

### 問題 4：operator ValueError — sinfo stderr 混入 stdout

**現象：** operator CrashLoopBackOff，traceback：`ValueError: invalid literal for int() with base 10: 'slurm_load_partitions: Unable to contact slurm controller (connect failure)\n0'`

**原因：** `kubernetes.stream` 的 exec 會將 stderr 和 stdout 合併回傳（`stderr=True, stdout=True`）。`_get_busy_nodes_exec` 的 sinfo 命令因 controller 還沒好而輸出 error 訊息到 stderr，與 awk 的 `0` 結果拼在一起，`int()` 轉換失敗。

**修法：**
1. sinfo 加 `2>/dev/null` 抑制 stderr。
2. 結果解析改為取最後一行（`lines[-1]`），即使有前置警告也不影響。

---

### 問題 5：bootstrap 腳本 `python3` 在 Windows 失敗（exit 49）

**現象：** bootstrap-phase1.sh 中 `python3 render-slurm-static.py` 回傳 exit code 49（Windows Store app stub 行為）。

**原因：** Windows 的 `python3` 在沒有安裝 Python 時會打開 Microsoft Store；即使裝了 Python，git bash 下 `python3` 可能不在 PATH。

**修法：** bootstrap 腳本改為先嘗試 `py -3`（Windows py.exe launcher），失敗才 fallback 到 `python3`：
```bash
if py -3 phase1/scripts/render-slurm-static.py 2>/dev/null; then
  true
else
  python3 phase1/scripts/render-slurm-static.py
fi
```

---

### 問題 6：verify-phase1.sh sinfo 時機太早

**現象：** pod Ready 後立刻 exec sinfo，但 slurmctld 還沒完全啟動，回傳 `Unable to contact slurm controller`。

**原因：** `kubectl wait --for=condition=Ready` 只確認 readinessProbe 通過（`pgrep -x slurmctld`），不保證 slurmctld 已完成初始化並接受連線。

**修法：** 加入 `scontrol ping` 重試等待（最多 90 秒）後再執行 sinfo。

---

---

## PDB + MPI 改進踩坑紀錄 (2026-04-10)

### 問題 1：`policy/v1` vs `policy/v1beta1`

**現象：** 在舊版本 Kind（K8s < 1.21）上套用 PDB manifest 時可能遇到 API version 錯誤。

**原因：** `policy/v1beta1/PodDisruptionBudget` 在 K8s 1.25 已刪除；現代版本只剩 `policy/v1`。

**修法：** 本專案 PDB 一律使用 `policy/v1`。若需要支援舊 K8s，改為 `policy/v1beta1`。

**確認版本：** `kubectl api-versions | grep policy`

---

### 問題 2：PDB 與 StatefulSet 縮容的關係

**常見誤解：** 認為 PDB 的 `maxUnavailable: 1` 會阻止 operator 把 replicas 從 4 降到 0。

**實際行為：**
- StatefulSet `replicas` 調整是 **Desired State**，K8s controller 會逐步刪除 Pod（最高優先）
- PDB 保護的是 **Voluntary Disruption**（如 `kubectl drain node`、節點升級）
- operator 調整 replicas = K8s 內部操作，**不受 PDB 約束**
- 結論：PDB 與 drain-then-scale 並不衝突；PDB 保護的是基礎設施層面，drain 保護的是 job 層面

---

### 問題 3：`MpiDefault=pmi2` 與 `mpi_pmi2.so` plugin 位置

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

### 問題 4：sbatch 中 heredoc 的 quoting 陷阱

**現象：** 在 bash 裡用 `echo "$SCRIPT" | sbatch` 提交，bash 展開變數導致 `$SLURM_PROCID` 等在提交端被展開而不是在 worker 端展開。

**原因：** 雙引號 `"$SCRIPT"` 讓 shell 展開變數；要在 worker 才展開，需用單引號或 `cat <<'EOF'`。

**修法：** verify-mpi.sh 使用 `cat <<'EOF'` 寫 batch script 到 pod，確保 `$SLURM_PROCID` 等變數在執行期才展開：
```bash
kubectl -n slurm exec pod/slurm-login-xxx -- bash -c "
cat > /tmp/job.sh <<'INNER'
#!/bin/bash
srun --mpi=pmi2 /bin/sh -c 'echo rank:\${SLURM_PROCID}'
INNER
sbatch --parsable /tmp/job.sh"
```

---

### 問題 5：`srun --mpi=pmi2` 在單節點多 task 的行為

**確認：** `--ntasks=2 --nodes=1` 加上 `srun --mpi=pmi2` 可以在同一個 worker pod 啟動兩個 MPI rank，`$SLURM_PROCID` 分別為 0 和 1。這對容器化 HPC 測試是最低門檻的 MPI 驗證方式，不需要 pod 間網路或 InfiniBand。

---

---

## Phase 5 Lmod 踩坑紀錄 (2026-04-10)

### 問題 1：`/etc/profile.d/slurm-modulepath.sh` 在 sbatch 裡不生效

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

### 問題 2：job output 在 worker pod，不在 login pod

**現象：** `cat /tmp/phase5-verify-$jid.out` 在 login pod 找不到檔案。

**原因：** Slurm 的 `--output` 路徑是在**執行 job 的 worker node** 上建立的。
沒有共享 filesystem（NFS/Lustre），output 不會自動傳回 login node。

**在真實 HPC：** 所有節點共享 NFS，`/home/user/` 或 `/scratch/` 上的 output 到處都能讀。

**本專案修法：** verify 腳本用 `sacct -P -o NodeList` 取得執行 job 的 worker pod 名稱，再 `kubectl exec` 去那個 pod 讀 output：
```bash
worker=$(sacct -j $jid -X -n -P -o "NodeList" | tr -d ' \r' | head -1)
kubectl exec pod/$worker -- bash -c "cat /tmp/output-$jid.out"
```

---

### 問題 3：sacct NodeList 欄位截斷

**現象：** `sacct -o NodeList` 回傳 `slurm-worker-c+`（18 字元被截斷）。

**原因：** sacct 預設欄位寬度不夠長，`slurm-worker-cpu-0` (18 chars) 超出。

**修法：** 加 `-P`（parseable mode），輸出以 `|` 分隔，無欄位寬度限制：
```bash
sacct -j $jid -X -n -P -o "NodeList"
# 輸出: slurm-worker-cpu-0
```

---

### 問題 4：Windows Git Bash (MINGW) 路徑自動轉換

**現象：** `kubectl exec pod/xxx -- cat "/tmp/file.txt"` 在 Windows Git Bash 裡執行時，pod 內回傳 `cat: 'C:/Users/.../AppData/Local/Temp/file.txt': No such file or directory`。

**原因：** Git Bash (MINGW) 把 `kubectl` 參數裡的 `/tmp/` 自動轉換成 Windows 暫存目錄路徑，然後 kubectl 把這個 Windows 路徑原封不動送進 Linux pod，pod 裡當然找不到。

**修法：** 設定 `MSYS_NO_PATHCONV=1` 停用路徑轉換：
```bash
MSYS_NO_PATHCONV=1 kubectl exec pod/xxx -- cat "/tmp/file.txt"
```

在 verify 腳本中封裝成 helper：
```bash
kexec() { MSYS_NO_PATHCONV=1 kubectl -n "$NAMESPACE" exec "$@"; }
```

**注意：** 此問題只影響 Windows Git Bash。Linux/macOS 上不存在。

---

### 問題 5：`module list` 輸出在 stderr

**現象：** sbatch job 中執行 `module list`，output file 裡看不到任何 module 資訊。

**原因：** Lmod 設計上把所有通知類輸出（`module list`、`module avail`、load/unload 訊息）送到 **stderr**，stdout 保持乾淨供 job 輸出使用。

**驗證：** 用 `--error` 對應的 `.err` 檔可以看到 `Currently Loaded Modules: 1) openmpi/4.1`。

---

## Phase 5 優先順序

| 項目 | 難度 | TA 價值 | 建議順序 |
|------|------|---------|---------|
| Helm Chart | 中 | 所有 TA（部署門檻決定採用率） | 第一 |
| Fair-Share metrics | 低 | AI 平台 TA（可視化多租戶） | 第二（與 Helm 並行） |
| Operator HA | 中 | SRE / 生產環境 TA | 第三 |
| OpenTelemetry | 高 | 所有 TA（差異化觀測） | 第四 |

Helm 和 Fair-Share metrics 可以平行進行（互不依賴），HA 需要先完成 Helm（讓 `replicas` 從 values 控制），OTel 放最後因為需要最多程式碼改動。
