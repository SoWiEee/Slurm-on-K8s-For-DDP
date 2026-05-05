# Scheduler Research — 為 Phase 6 預備的功課

> 對應問題（2026-05-05）：「兩張 RTX 4070 各切 4×25% MPS slot；A=3 slot@GPU0 + B=1 slot@GPU1，新進 C 要 2 slot — 能否把 B 搬到 GPU0、把 GPU1 空出來給 C？」這份文件整理三件事：
>
> 1. Slurm 自己的調度旋鈕能做到哪一步、bin-packing 是不是改設定就行
> 2. GCP / AWS / K8s 主流批次平台怎麼處理同一類問題
> 3. 學術界（含 DRL）的研究現況、為什麼產線基本沒在用 DRL 調度
>
> 結論先講：**Slurm 改設定能做到「新 job 到來時的 bin-packing」，但做不到「runtime 把跑到一半的 job 搬走」**。後者沒有任何主流系統用 DRL 在做，做的人都是用 application-level checkpoint + preempt/requeue（最有名的是 MSR 的 Gandiva）。

---

## 1. Slurm 內建的調度旋鈕

### 1.1 三個必懂的設定軸

Slurm 的調度行為由三組正交設定決定，全部寫在 `slurm.conf`：

| 設定 | 作用 | 我們目前的值 |
|------|------|--------------|
| `SchedulerType` | 主調度器演算法 | `sched/backfill`（預設） |
| `SelectType` | 資源 fit 演算法 | `select/cons_tres`（GPU/MPS 必需） |
| `SelectTypeParameters` | fit 演算法的細部策略（spread / pack / 各種變體） | `CR_Core` |
| `PriorityType` | job 排序方式 | `priority/basic`（FIFO） |
| `PreemptType` / `PreemptMode` | 是否允許 preempt | 未設定 = 不 preempt |

### 1.2 Bin-packing 在 Slurm 怎麼做

**跨節點 bin-packing**（把多個小 job 塞到同一個 node）：

```ini
SelectTypeParameters=CR_Core,CR_Pack_Nodes
```

`CR_Pack_Nodes` 讓 cons_tres 在 fit 一個 job 時，**優先選已經有負載的 node**，目的是讓沒負載的 node 完整空出來給未來的大 job。預設是 `CR_LLN`（Least Loaded Nodes）— spread 平均分散，反過來。

對我們這套（2 GPU node）的影響很小，因為節點數本來就少。但概念是對的：「優先填已用的卡 → 騰出空卡給大 job」。

**節點內 GPU bin-packing**（多個 mps job 塞同一張卡）：

`select/cons_tres` 的 GRES allocator 預設就**會**這樣做 — 它會先嘗試把新 job 的 mps slot 塞到「該 node 上 mps 已被部分使用的 GPU」，只有放不下時才動下一張卡。所以對我們的情境，**第一次提交 C 時** Slurm 會自己嘗試 bin-pack；它放不下純粹是因為 GPU0 真的剩下 1 slot < C 的 2 slot 需求。

要進一步調這個層次的細節，可在 `gres.conf` 寫 GPU topology：

```
NodeName=worker-gpu Name=gpu Type=rtx4070 File=/dev/nvidia0 Cores=0-1
NodeName=worker-gpu Name=gpu Type=rtx4070 File=/dev/nvidia1 Cores=2-3
```

`Cores=` 把 GPU 綁到特定 CPU core，再配 `--gres-flags=enforce-binding` 可以讓 cons_tres 把 CPU 與 GPU 一起 pack。**但這只影響 fit 時的選卡，不影響已經在跑的 job。**

### 1.3 Backfill — Slurm 的二級救援

`sched/backfill` 在主排程後跑：FIFO 排不下時，從 queue 後面挑「不會延誤前面 job 開始時間」的小 job 提早跑。對 fragmentation 場景幫助有限 — 它讓 C 等 A 結束後跑，但**不會主動把 B 搬走**。

可調的：

```ini
SchedulerParameters=bf_window=1440,bf_resolution=60,bf_continue,bf_max_job_test=1000
```

`bf_window` 是 backfill 看多遠的未來（分鐘），`bf_max_job_test` 是每輪檢查多少 job。對小叢集調大這些不痛，對大叢集會吃 slurmctld CPU。

### 1.4 Preempt — 唯一能「動到 running job」的內建機制

Slurm 提供 4 種 preempt 模式，全寫在 partition / QoS 上：

| Mode | 行為 |
|------|------|
| `OFF` | 不 preempt（預設） |
| `CANCEL` | 直接殺掉低優 job |
| `REQUEUE` | 殺掉但丟回 queue 重排 |
| `SUSPEND` | 凍結 process（SIGSTOP），保留記憶體 — **GPU job 用這個基本沒用**，CUDA context 還佔著 VRAM |
| `GANG` | 時分多工，多個 job 輪流跑 |

GPU 場景能用的只有 `REQUEUE`：殺掉 B、放出 1 slot、讓 C 跑、B 等下一輪 — **B 必須自己會 resume**（讀 checkpoint）。Slurm 不會幫你保 GPU memory state。

設定組合：

```ini
PreemptType=preempt/qos
PreemptMode=REQUEUE
PreemptParameters=reclaim_licenses
PartitionName=gpu-rtx4070 PriorityTier=10 PreemptMode=REQUEUE
```

加上 QoS：
```bash
sacctmgr add qos high Priority=1000
sacctmgr add qos low  Priority=100
sbatch --qos=high ...    # 可以 preempt
sbatch --qos=low  ...    # 會被 preempt
```

### 1.5 Slurm 內建調度的能力邊界

| 能做到 | 改設定即可？ |
|--------|---------------|
| 新 job 進來時挑「最填得滿」的 node/GPU | ✅ `CR_Pack_Nodes` + cons_tres |
| 大 job 等待時讓小 job 先插隊（不延後大 job） | ✅ `sched/backfill` 預設開 |
| 高優先 job 強制踢走低優先 job（kill + requeue） | ✅ `PreemptType=preempt/qos` + `REQUEUE` |
| Job suspend 後保留 GPU memory state | ❌ CUDA 不支援 |
| **將跑到一半的 job 從 GPU1 搬到 GPU0**（live migration） | ❌ 沒有任何內建機制 |
| 跑到一半時主動 evict 低優 job 來解 fragmentation | ❌ Slurm 不會主動 — 必須外部觸發 |

最後兩列才是使用者問題的核心。Slurm 做不到，**任何主流叢集排程器都做不到**（K8s / Kueue / Volcano 也一樣）。

---

## 2. GCP / AWS / K8s 怎麼處理同類問題

### 2.1 AWS ParallelCluster — Slurm 包一層

直接用 Slurm，調度策略和上面一樣。AWS 加值的是 **autoscaling 對接 EC2**：node 不夠時自動開 instance，閒置自動關。對 fragmentation 的態度是「反正 instance 多到不會 fragment，scale-out 就好」。

對應到我們：operator 已經做了 K8s 版的 scale-out。但**單機兩張 GPU 的場景沒法靠 scale-out 解** — 你不能變出第三張卡。

### 2.2 AWS Batch / GCP Batch — 託管批次

兩者都是把 job 塞到 ECS / GCE 的 placement group，placement strategy 有兩種：

- **`bin-pack`**：填滿一台再開下一台（省錢）
- **`spread`**：分散（防止單點故障）

這是 **instance / VM 層**的 bin-pack，不是 GPU 層。GPU 切片都是「整卡或 MIG slice」，沒人在 GPU 內部做 fragmentation 重整 — 直接靠「用大量 instance 把問題稀釋掉」。

### 2.3 GKE / Kueue / Volcano — K8s 原生批次

K8s 的 default scheduler `kube-scheduler` 有兩個關鍵 score plugin：

| Plugin | 行為 |
|--------|------|
| `NodeResourcesFit` (LeastAllocated) | 預設，spread |
| `NodeResourcesFit` (MostAllocated) | bin-pack |
| `NodeResourcesBalancedAllocation` | CPU/memory 用量平衡 |

GKE 上開 bin-pack 是改 scheduler config，跟 Slurm `CR_Pack_Nodes` 概念一致。

**Volcano**（CNCF，Bytedance / 華為主推）和 **Kueue**（K8s SIG-Scheduling）是專門給批次工作的 K8s 排程器：

- Volcano：plugin 化的 scheduler，有 `binpack`、`gang`、`fairshare`、`reservation`、`preempt` plugin。**Gang scheduling**（DDP 必需）是它最大賣點 — 確保 N 個 worker pod 同時 ready 才開跑。
- Kueue：較新，主打 hierarchical quota + cohort（部門配額），支援 workload preemption。

兩者對 GPU fragmentation 的處理：**preempt + requeue**，跟 Slurm 一模一樣。沒有 live migration。

### 2.4 一個共通結論

GCP / AWS / K8s 的所有產線排程器面對「fragmentation 解決方案」都是兩條路：

1. **靠規模稀釋**：node 多到 fragment 不痛
2. **preempt + requeue**：殺低優讓高優先跑，job 自己處理 resume

**沒有人在產線上做 GPU live migration**。原因下節說。

---

## 3. 學術研究：Gandiva 等 GPU-aware 排程器

### 3.1 Gandiva（OSDI'18，Microsoft Research）

直接對應使用者問題的論文。Gandiva 觀察到：

- DL training job 是 **iterative**（每個 mini-batch 跑類似計算）
- 在 mini-batch 邊界上，GPU memory 可以被 dump 出來
- 因此可以做 **introspective migration**：把 job 在 GPU 之間搬，目的就是解 fragmentation

機制：

1. 每隔 N 個 mini-batch，framework hook（PyTorch/TF）暫停訓練、把 model + optimizer state 寫到 host memory
2. 排程器觸發 migration：在新 GPU 上 spawn 新 worker，host memory 讀回來、restore
3. 整個過程秒級（不像普通 checkpoint 是分鐘級）

**為什麼 Gandiva 沒進產線：**
- 需要修改 framework（Gandiva 改 CNTK，後續版本改 PyTorch），不通用
- 對非 iterative job（HPC、模擬、推論）完全沒用
- 跟 NCCL / DDP collective 互動複雜（migration 中其他 worker 要等）
- MSR 內部用，沒釋出生產級 OSS 版本

### 3.2 後續研究脈絡

| 系統 | 會議年份 | 核心想法 | 跟 fragmentation 的關係 |
|------|---------|---------|--------------------------|
| Gandiva | OSDI'18 | Introspective migration | ✅ 直接解 |
| Tiresias | NSDI'19 | Age-based 排程（job 跑越久優先序越低） | ⚠️ 間接（讓老 job 自然結束釋放） |
| Themis | NSDI'20 | ML job 的 fairness | ❌ |
| Gavel | OSDI'20 | 異質 GPU（V100/A100 混跑）排程 | ⚠️ 透過 throughput 模型挑卡 |
| Pollux | OSDI'21 | 同時調 batch size + GPU 分配 | ⚠️ 動態 rescale 而非 migrate |
| Synergy | OSDI'22 | 多資源（GPU + CPU + memory）感知 | ❌ |
| Lyra | EuroSys'23 | 推論 + 訓練混合，動態借用 | ✅ preempt 推論讓訓練先跑 |
| Sia | SOSP'23 | Heterogeneous GPU 排程 + adaptive | ⚠️ |

**共同模式：所有把 migration / preemption 做認真的論文，都依賴 application 層（framework）配合**。沒有純排程器層級的解法。

### 3.3 為什麼 MPS 場景特別難

Gandiva 那套是 **GPU 整卡獨佔** 假設下做的：dump 一個 process 的整張 GPU state、restore 到另一張。MPS 共享下：

- 一張卡上多個 client 共用同一個 CUDA context
- 你要 dump 的不是「整張 GPU」，而是「某個 client 的 partial state」
- NVIDIA 的 cuda-checkpoint 工具（CUDA 12.4+）開始支援 process 層級 checkpoint，但**對 MPS client 的支援還在 alpha**

實務上：**MPS + migration 目前沒有任何成熟方案，學術界也沒有專門針對這個的論文**。

---

## 4. 深度強化學習（DRL）做調度

### 4.1 代表作

| 論文 | 年份 | 場景 | 是否上線 |
|------|------|------|----------|
| DeepRM | HotNets'16 (MIT) | 通用 job scheduling | ❌ 玩具規模 |
| Decima | SIGCOMM'19 (MIT) | Spark DAG scheduling | ❌ 學術 |
| Park | NeurIPS'19 (MIT) | RL for systems benchmark | ❌ |
| DL2 | TPDS'21 (港大) | DL job 放置 | ❌ |
| Harmony | SoCC'19 | DL cluster scheduling with DRL | ❌ |
| RLScheduler | SC'20 | HPC batch scheduling RL | ❌ |

### 4.2 為什麼產線沒人用

歸納各論文 + 產業實務（Borg paper / Kubernetes scheduler 設計）：

1. **訓練成本高**：scheduler 要在 cluster trace 上訓練數千~數萬 episode，每次叢集規模或工作負載分布變化（distribution shift）就要重訓。Kubernetes scheduler 的啟發式可以「設定即生效」。
2. **無 SLA 保證**：DRL policy 是黑箱，沒法證明「不會餓死某個 job」「不會 starvation」。產線需要硬保證。
3. **可解釋性差**：oncall 看到 job 排不上時，DRL 給不出原因。priority + fit + score 可以一行一行解釋。
4. **啟發式已經夠好**：Google Borg、Kubernetes、Slurm 用 priority + multifactor weight 的啟發式可以做到 95% 場景的最優或近優。剩下 5% 改 weight 就好。
5. **災難回滾困難**：啟發式調 weight 是線性可預期的，DRL policy 換版本可能造成意料外的全域性能改變。

### 4.3 真實的「AI for systems」在產線怎麼用

Google 有用 ML 在 **資料中心冷卻**、**容量規劃**、**負載預測** 上（這些是長時間尺度、可離線決策的問題）。**即時排程決策幾乎沒有人用 DRL**。最接近的是：

- 用 ML 預測 job 執行時間（取代使用者填的 wall time），餵給傳統排程器做更準的 backfill — 例如 ALCF / NERSC 有研究用 GBDT 預測 runtime
- 用 ML 預測 GPU 故障 / preemption 風險，影響 placement — Microsoft 的 Singularity 有部分這樣做

---

## 5. 我們 Phase 6 可以做什麼（具體）

按可行性排序：

### 5.1 P0：把 Slurm 內建設定先打開（最簡單，立刻有效）

```ini
# slurm.conf — 在 chart values.yaml 加旋鈕
SelectTypeParameters=CR_Core,CR_Pack_Nodes
SchedulerParameters=bf_window=720,bf_resolution=30,bf_continue
PriorityType=priority/multifactor
PriorityWeightAge=1000
PriorityWeightFairshare=0
PriorityWeightJobSize=500
PriorityWeightPartition=1000
PriorityWeightQOS=2000
PreemptType=preempt/qos
PreemptMode=REQUEUE
```

對應到 `chart/values.yaml` 加：
```yaml
slurm:
  selectTypeParameters: "CR_Core,CR_Pack_Nodes"
  schedulerParameters: "bf_window=720,bf_resolution=30,bf_continue"
  preempt:
    enabled: false   # 預設關，因為要 application 配合 checkpoint
    type: preempt/qos
    mode: REQUEUE
```

**收益：** 新 job 進來時 cons_tres 會優先 pack，部分 fragmentation 自然減少。
**做不到：** 解 runtime fragmentation。

### 5.2 P1：Application-level checkpoint + 外部 evictor

Operator 加一個「fragmentation detector」迴圈：

1. 每輪查 squeue + sinfo 算 GPU slot fragmentation（pending high-prio job vs. running low-prio job）
2. 如果有解（kill 某個 low-prio job 能讓 pending 跑起來），呼叫 `scontrol requeue <jobid>`
3. 被 requeue 的 job 由 sbatch 模板處理 resume 邏輯（讀 `/shared/checkpoints/$SLURM_JOB_NAME/latest.pt`）

**這正是 Gandiva 的簡化版** — 沒做 introspective live migration，只做「kill + 自願 resume」。配合 Phase 7 OTel trace 可以量化：fragmentation 發生頻率、resume 成本、整體 throughput 改善。

### 5.3 P2：自訂 priority plugin 或 backfill 變體

Slurm 開放 C plugin 介面（`src/plugins/priority/`、`src/plugins/select/`）。可以寫一個 priority plugin 加進「考慮 GPU topology / NCCL 親和性」的因子。

但 C plugin 開發成本高、debug 困難、要 build 進 controller image。**除非 P0 + P1 的資料證明這條路有顯著 ROI，否則不建議做**。

### 5.4 不建議：DRL 排程器

理由見 §4.2。我們叢集規模太小（< 10 GPU），啟發式天花板還很高、收益空間不夠付 DRL 訓練成本。如果要做 ML，應該做的是 **runtime 預測**（給 backfill 用），而不是排程決策本身。

---

## 6. References

### Slurm 文件
- Slurm `slurm.conf` — https://slurm.schedmd.com/slurm.conf.html（`SelectTypeParameters`、`SchedulerParameters`）
- Slurm Preemption — https://slurm.schedmd.com/preempt.html
- Slurm cons_tres — https://slurm.schedmd.com/cons_tres.html

### K8s 批次排程器
- Volcano — https://volcano.sh/
- Kueue — https://kueue.sigs.k8s.io/
- kube-scheduler scoring — https://kubernetes.io/docs/reference/scheduling/config/

### 學術論文
- **Gandiva**: Xiao et al., "Gandiva: Introspective Cluster Scheduling for Deep Learning", OSDI 2018
- **Tiresias**: Gu et al., "Tiresias: A GPU Cluster Manager for Distributed Deep Learning", NSDI 2019
- **Pollux**: Qiao et al., "Pollux: Co-adaptive Cluster Scheduling for Goodput-Optimized Deep Learning", OSDI 2021
- **Gavel**: Narayanan et al., "Heterogeneity-Aware Cluster Scheduling Policies for Deep Learning Workloads", OSDI 2020
- **Sia**: Jayaram Subramanya et al., "Sia: Heterogeneity-aware, goodput-optimized ML-cluster scheduling", SOSP 2023
- **DeepRM**: Mao et al., "Resource Management with Deep Reinforcement Learning", HotNets 2016
- **Decima**: Mao et al., "Learning Scheduling Algorithms for Data Processing Clusters", SIGCOMM 2019

### Google / Microsoft 產線
- Verma et al., "Large-scale cluster management at Google with Borg", EuroSys 2015
- Tirmazi et al., "Borg: the next generation", EuroSys 2020
- Microsoft Singularity — https://arxiv.org/abs/2202.07848
