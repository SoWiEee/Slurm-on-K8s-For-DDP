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

1. 目前這個專案，能不能在同一台 `cpu-worker` 上同時跑多個 job，藉此提高 CPU 利用率。
2. `gpu-worker` 目前能不能做到類似的共享。
3. 若不能，實務上有哪些開源方案或可行方法。
4. 以上判斷都要放在 **目前是 kind（Kubernetes in Docker）模擬環境** 這個前提下理解。

## 先講結論

### CPU worker

**可以，但要分成「排程層可不可以」與「隔離層有沒有做紮實」兩件事來看。**

目前 repo 的 `slurm.conf` 採用：

- `SelectType=select/cons_tres`
- `SelectTypeParameters=CR_Core`
- 每個 CPU worker 宣告 `CPUs=4`

這代表 Slurm 會把 CPU 當成 consumable resource 來分配。只要多個 job 的 CPU 請求總和沒有超過該 node 的可用 CPU，**Slurm 是可以把多個 job 放到同一台 worker 上的**。Slurm 官方文件明確說明，使用 consumable resource（cons_tres）時，CPU 會配置給 job；不同 job 是否能共用同一顆 CPU，則取決於 OverSubscribe 設定。預設 `OverSubscribe=NO` 時，不會讓兩個 job 共用同一顆 CPU，但同一台 node 上仍可同時承載多個 job，只要它們使用的是不同 CPU 資源。citeturn832572search6turn998774search18turn832572search15

換句話說，**在目前這份設定下，同一台 `cpu-worker` 跑多個 job 是可能的，而且這其實就是提高單機 CPU 利用率的預設方向**。例如：

- job A 請求 1 CPU
- job B 請求 1 CPU
- job C 請求 2 CPU

在 `CPUs=4` 的 worker 上，這三個 job 可以同時被排進去，總計用滿 4 CPU。這不需要 `OverSubscribe`。`OverSubscribe` 只在你想讓多個 job **共用同一批 CPU** 時才需要。citeturn998774search18turn832572search15

### 但目前 repo 的限制很大

雖然 Slurm 排程層面允許 packing，但目前 repo 還沒有把 CPU/memory 隔離做完整。從現有 `slurm.conf` 可見：

- `TaskPlugin=task/none`
- `ProctrackType=proctrack/linuxproc`

這表示目前沒有啟用 Slurm 常見的 cgroup/task 隔離路徑。結果是：

- Slurm **會記帳與配置** CPU 數量
- 但它**不一定會強制把 job 嚴格限制在那幾顆 CPU 上**
- 多個 job 都跑在同一個 worker pod 裡時，Linux 行程層面可能彼此搶 CPU，而不是像正式 HPC 節點那樣有明確 cpuset/cgroup 約束

所以答案不能講太漂亮。**目前 CPU worker 的「多 job 共存」在排程語意上是可行的，但在 kind 模擬環境下，資源隔離與效能可預測性偏弱。**

### GPU worker

**目前不應假設可以安全地在同一張 GPU 上同時跑多個 GPU job。**

原因有兩層。

第一層是 Slurm / K8s 的資源模型。repo 目前把 GPU worker 宣告成：

- `Gres=gpu:a10:1` 或 `Gres=gpu:h100:1`

這是典型的「一張卡就是一個 consumable GPU resource」配置。若 job 請求 `--gres=gpu:a10:1`，那張 GPU 在 Slurm 看來就會被整張配置給那個 job。這種配置預設不是拿來做多 job sharing 的。Slurm 文件也指出，像 GPU 這類 GRES/TRES 會被當成可分配資源記帳與分配。citeturn998774search7turn832572search18

第二層是 Kubernetes / 裝置插件語意。NVIDIA 的 k8s device plugin 預設是把 GPU 以 extended resource 方式暴露給容器，一般語意是一個請求拿到一個 GPU 資源單位。若沒有另外啟用 time-slicing、MIG 或其他 sharing 機制，就不應把「多個 GPU job 在一張卡上共享」當成預設可行行為。citeturn998774search4turn832572search2turn998774search6

因此，**目前這個 repo 的 GPU worker 比較接近「每張 worker pod 對應一張獨占 GPU」的模型，不是 GPU sharing 模型。**

## 那目前預設行為到底會怎樣

### CPU worker 的預設行為

若 job 沒有把整台 node 吃滿，**同一台 CPU worker 可以被排入多個 job**。但前提是 job 本身要正確申請 CPU，例如：

- `--cpus-per-task=1`
- `--ntasks=1`
- 或總 CPU 需求沒有超過 node 的 `CPUs=4`

若你提交的 job script 沒有清楚聲明 CPU 需求，或應用程式自己在容器裡開太多 threads，最後實際上可能會出現：

- Slurm 認為只分了 1 CPU
- 應用程式卻在 worker pod 裡吃超過 1 CPU

這是目前 repo 因為沒有 cgroup/task plugin 而留下的風險。

### GPU worker 的預設行為

**目前比較接近不能共享。**

只要 job 申請了 `gpu:a10:1` 或 `gpu:h100:1`，那個 GPU 資源就會被當成完整的一份配置掉。要讓多個 job 共用同一張 GPU，需要額外導入 sharing 機制，現在 repo 沒有做。

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

# CPU / GPU 資源分配與多 Job 共用調查（2026-03-28）

1. Job 是否能指定要用多少 CPU 和 GPU？
2. 一台 worker 的 CPU cores 是否能同時給兩個不同的 job 使用？
3. GPU 是否能被多個 job 共用？

## 相關設定（slurm-static.yaml / slurm.conf）

```
SelectType=select/cons_tres
SelectTypeParameters=CR_Core
TaskPlugin=task/none
GresTypes=gpu
```

每台 worker 節點宣告：
```
CPUs=4  Sockets=1  CoresPerSocket=2  ThreadsPerCore=2  RealMemory=3500
```

GPU worker 額外宣告（gres.conf）：
```
Gres=gpu:a10:1   # A10 pool，每台 1 張
Gres=gpu:h100:1  # H100 pool，每台 1 張
File=/dev/null   # Kind 環境模擬，無真實硬體
```

## Job 指定資源的方式

標準 Slurm 旗標均支援：

```bash
# CPU
#SBATCH --cpus-per-task=2   # 每個 task 要 2 cores
#SBATCH --ntasks=4          # 4 個 task（共 8 cores）

# GPU
#SBATCH --gres=gpu:a10:1    # 要 1 張 A10
#SBATCH --constraint=gpu-a10
```

## CPU 多 Job 共用分析

`select/cons_tres` + `SelectTypeParameters=CR_Core` 啟用 **Consumable Resources 模式**，Slurm 以 core 為單位追蹤每台節點的資源消耗。每台 CPU worker 有 4 個可分配 CPU slot，可同時排入多個 job：

| Job A | Job B | 同一 worker 可行？ |
|-------|-------|------------------|
| `--cpus-per-task=2` | `--cpus-per-task=2` | ✅ 各佔 2 cores，合計 4 |
| `--cpus-per-task=3` | `--cpus-per-task=2` | ❌ 超過 4 cores，排到不同 worker |
| `--cpus-per-task=4` | 任意 | ❌ 整台 worker 被佔滿 |

**結論：排程語意層面可以做到 CPU packing（多 job 共用同一 worker）。**

## GPU 多 Job 共用分析

每台 GPU worker 只宣告 `Gres=gpu:a10:1`（1 張）。GRES 是整數消耗，無分數分配。

- Job A 請求 `--gres=gpu:a10:1` → 佔用整張 GPU
- Job B 同樣請求 → 必須等 A 結束，或排到另一台 worker

**結論：GPU 不支援多 Job 共用，每台 worker 同時只能跑一個 GPU job。**

## 重要限制：TaskPlugin=task/none

`TaskPlugin=task/none` 代表 Slurm 只在**排程計算層面**追蹤 core 數量，但不執行任何 CPU binding 或 cgroup 隔離。兩個 job 被排到同一 worker 後，OS 層面的 process 可跑在任意 CPU 上，沒有強制 pinning。

在真實 HPC 環境通常改為 `TaskPlugin=task/cgroup` 來強制隔離。但在本 Kind/Docker 環境中，cgroup 設定會疊加在 K8s / container runtime 的抽象之上，只適合驗證排程控制流，不適合測試效能隔離。

## 調查結論

| 問題 | 結果 |
|------|------|
| Job 能指定 CPU 數量？ | ✅ `--cpus-per-task`、`--ntasks` |
| Job 能指定 GPU 數量？ | ✅ `--gres=gpu:a10:1` |
| 同一 worker CPU 能讓兩個 job 共用？ | ✅ 排程層面可以（`CR_Core` consumable） |
| 是否有 CPU 實體隔離？ | ❌ `TaskPlugin=task/none`，無 binding/cgroup |
| 同一 worker GPU 能讓兩個 job 共用？ | ❌ 每台只有 1 GPU，整數消耗 |
| GPU GRES 是真實硬體？ | ❌ `File=/dev/null`，Kind 環境純排程模擬 |

---

# Phase 4 (DDP)

## 背景與動機

本專案的核心主張是：Kubernetes 擅長彈性伸縮與容器管理，但對 HPC / AI 訓練工作負載的語意支援有限；Slurm 擅長批次排程與叢集治理，但傳統部署偏靜態、對雲端彈性不夠友善。Phase 4 的目標是把這個系統實際用在 **PyTorch DDP 分散式訓練**情境，並補上可觀測性，讓橋接過程可以被量測與展示。

---

## 必做項目

### 1. Slurm REST API 取代 kubectl exec（穩定性關鍵）

**現狀問題：** operator 目前透過 `kubectl exec` 進入 controller pod 執行 `squeue`、`sinfo`、`scontrol`，每次 exec 都有 fork 開銷，遇到 slurmctld 暫時忙碌時直接 timeout，是目前 operator 不穩定的主要根源。

**目標改法：** 在 controller image 啟動 `slurmrestd`，operator 改成呼叫 REST API：

```
目前：operator → kubectl exec pod/slurm-controller-0 → squeue（每次 exec 有啟動開銷）
改後：operator → HTTP GET http://slurm-controller:6820/slurm/v0.0.39/jobs → JSON 解析
```

**參考來源：** `github.com/SlinkyProject/slurm-exporter`（SchedMD 官方 exporter 完全基於此 API）

**收益：**
- 消除 N+1 exec 問題的根本原因
- REST API 回應比 exec + CLI parse 快且穩定
- 同一個 slurmrestd endpoint 可同時供 operator 查詢與 Prometheus scrape，Phase 4 monitoring 不需要額外的 exec

---

### 2. Worker Image 加入 PyTorch（DDP 前置條件）

目前 worker image 只有 Slurm + Munge，要跑 DDP 需要加入 PyTorch：

```dockerfile
# phase1/docker/worker/Dockerfile 加入
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
# Kind 環境無真實 GPU，先用 CPU 版本驗證 DDP 控制流
```

---

### 3. 標準 DDP sbatch 腳本

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

### 4. SIGTERM → Checkpoint 儲存串接 Checkpoint Guard

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

### 5. Prometheus + Grafana 監控（Phase 4 主體）

詳細規格見 `docs/monitoring.md`。核心是三層 metrics：

| 來源 | 取得方式 | 關鍵指標 |
|------|---------|---------|
| slurm-exporter | scrape slurmrestd（REST API） | queue_pending, nodes_idle |
| kube-state-metrics | K8s 原生 | StatefulSet replicas, Pod ready |
| operator 自定義 | prometheus_client HTTP server | scale_events, guard_blocks |

**參考來源：** `github.com/SlinkyProject/slurm-exporter`（REST API 驅動）、`github.com/vpenso/prometheus-slurm-exporter`（exec 驅動，較易入門）

---

## 建議加入的改進（可選）

### 6. Job 提交前健康檢查（pre-check）

**參考來源：** Character.ai Slonk（`blog.character.ai/slonk/`）

在 sbatch 腳本開頭加一個 pre-check stage，確認每個分配到的節點 `/shared` 掛載正常後再開始 torchrun：

```bash
# sbatch 前置 pre-check
srun bash -c "test -d /shared && echo ok || echo fail" | grep -v ok && exit 1
```

收益：降低 DDP job 因 NFS 掛載異常或節點網路問題在訓練中途失敗的機率。

---

### 7. Elastic DDP（動態 world size）

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

### 8. Soperator reconciliation loop 設計參考

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
