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

### 4.2 為什麼**產線**沒人用（學術專題請跳到 §7）

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

### 5.4 學術專題：DRL / 公式化 / ML runtime 預測

**這個方向對產線不划算，但對校內專題完全可行**，而且本身是一條成熟的研究脈絡。詳細展開見 §7、§8、§9。

精簡結論：**先寫公式，再用 ML/DRL 取代寫不出公式的部分**。對 thesis 來說，先做 §8（custom priority plugin，公式化排程）拿到一個可解釋的 baseline，再做 §9（ML runtime 預測）強化 backfill，最後才考慮 §7（DRL 排程 policy）— 這個順序的工程成本遞增，可發表的 novelty 也遞增。

---

## 7. 學術專題視角：DRL 與公式化排程的研究脈絡

> 給校內專題 / thesis 用的版本。產線不用 ≠ 學術沒做 — 事實上 **GPU 排程是 MLSys 社群最熱門的子領域之一**，過去 8 年（2018–2025）光 OSDI/SOSP/NSDI/EuroSys/ATC/SC/SoCC/SIGCOMM/MLSys/TPDS 加起來有超過 30 篇相關論文。

### 7.1 「推導一個公式」其實是主流做法 — 叫 Analytical / Model-based Scheduling

你的直覺（從 mps / vram / bw 推一個 score）就是 Gavel、Pollux、Themis、Optimus 等論文的核心。它們不是 DRL，是 **analytical modeling + optimization**：

| 論文 | 公式長什麼樣 | 求解方法 |
|------|---------------|----------|
| **Gavel** (OSDI'20) | `throughput[i,j] = profile(job_i, gpu_type_j)`，矩陣化 | LP（Linear Programming）求最大 throughput / 最公平分配 |
| **Pollux** (OSDI'21) | `goodput = throughput × statistical_efficiency(batch_size)` | 線上 fit + greedy reallocation |
| **Themis** (NSDI'20) | `finish-time fairness ρ = T_shared / T_isolated` | 拍賣（partial allocation auction） |
| **Optimus** (EuroSys'18) | `loss(t) ≈ a / (b·t + c)`，每個 job 線上擬合 | Marginal utility 的 greedy |
| **AFS** (NSDI'21) | `share = utility / contention` | 數值優化 |
| **Synergy** (OSDI'22) | 多資源 utility 模型（GPU + CPU + memory + storage bw） | LP + heuristic |
| **Sia** (SOSP'23) | Heterogeneous GPU 的 throughput 預測 + adaptive | bandit-style |

**對你的 (mps, vram, bw) 公式想法的對應：**

```
score(job J on GPU g) =
    α · (mps_slots_fit(J, g) / mps_slots_request(J))     # MPS 容量配適
  + β · (vram_available(g) - vram_required(J))           # VRAM 餘裕
  + γ · bandwidth_to_other_workers(g, J.peers)            # NCCL collective 親和性
  - δ · fragmentation_penalty(g after placing J)          # 放完後碎片化代價
  + ε · gang_readiness(J)                                 # DDP gang 完整度
```

這就是一個 score function — 給每個 (job, placement) 算分，挑最高的。係數 α/β/γ/δ/ε 怎麼定？三條路：

1. **手調**：寫個 sensitivity analysis（grid search），跑 trace replay 找最佳值。**最快、最可解釋、最容易寫進論文 evaluation 章節**。
2. **線上學習（contextual bandit）**：把 α–ε 當 learnable weight，用 EXP3 / LinUCB 等簡單演算法線上更新。比 DRL 簡單一個量級，但有 regret bound 可分析。
3. **DRL**：把整個 (state → action) 學起來，根本不寫公式。最有 novelty 但工程成本最大。

### 7.2 純 DRL 排程的學術論文（你問的那塊）

**真的有人在做，而且不算少**：

| 論文 | 年份 | 場景 | 演算法 | 重點 |
|------|------|------|---------|------|
| DeepRM | HotNets'16 (MIT) | 通用 cluster | Policy Gradient (REINFORCE) | 開山作，2D grid state |
| Decima | SIGCOMM'19 (MIT) | Spark DAG | GNN + RL | DAG state encoding 是貢獻 |
| Harmony | SoCC'19 (港大) | DL cluster | Actor-Critic | Interference-aware placement |
| DL2 | TPDS'21 (港大) | DL job | Offline supervised + online RL | warm-start 解 cold start 問題 |
| RLScheduler | SC'20 | HPC batch | PPO + Kernel-based policy | 直接對 Slurm trace 訓練 |
| Horus | TPDS'21 | GPU cluster | Interference prediction + heuristic | 比較 ML + heuristic |
| MLFS | INFOCOM'22 | DL cluster | DRL | 異質 GPU + locality |
| GPARS | MLSys'24 | LLM serving | RL + bandit | 推論場景 |
| **Lucid** | ASPLOS'23 | DL cluster | Non-intrusive profiling + ML | 黑箱 job 也能調度 |

**為什麼產線不用 ≠ 學術做不出來**：

- 產線：要 SLA、要可解釋、要面對 distribution shift — DRL 都不擅長
- 學術：在固定 trace 上比 baseline、比 utilization、比 JCT — DRL 完全可以贏

### 7.3 給你的具體 thesis 路徑

**最務實的研究設計**（可以 1 學期做完、有實作有評估）：

```
題目：MPS-aware Scheduling on Slurm/Kubernetes for Multi-tenant DL Workloads
       — A Score-based Approach with Optional RL Tuning

第一階段（必做、佔 thesis 60%）：公式化 + 實作
  1. 在現有 Slurm + GPU Operator MPS 上，定義 (mps, vram, bw, fragmentation) score function
  2. 用 Slurm submit plugin (Lua) 或 custom priority plugin (C) 把 score 接進 Slurm
  3. 在真機跑 small-scale workload（10–50 個 job mix）vs. FCFS / 預設 backfill 比

第二階段（可選、佔 thesis 30%）：用 trace replay 比較
  1. 拿公開 trace（Microsoft Philly 或 Alibaba PAI，見 §7.4）
  2. 寫一個簡單模擬器（DiscoSched / 自寫 Python event sim）
  3. 跑公式版 vs. DRL 版 vs. baselines，比 JCT、makespan、utilization、fairness

第三階段（加分、佔 thesis 10%）：DRL 取代部分手調
  1. 把 score function 的 α–ε 當 RL action，state = cluster snapshot
  2. PPO + 公開 trace 訓練 policy
  3. 對比公式 + RL 與純公式
```

**為什麼這個切法好**：

- **第一階段** 給你「真實系統的工程貢獻」 — Slurm + GPU Operator MPS 上幾乎沒人做過 score-based scheduling
- **第二階段** 給你「可信的 evaluation」 — 真機規模太小，trace replay 才能做有統計意義的比較
- **第三階段** 給你「ML novelty」 — 但不會壓在 thesis 主軸上，做不完也不影響畢業

### 7.4 學術界常用的公開 GPU trace（你 evaluation 要用）

| Trace | 來源 | 規模 | 連結 |
|-------|------|------|------|
| **Microsoft Philly** | OSDI'18 Gandiva 釋出 | 2 個月、~400 GPU、3000+ jobs | https://github.com/msr-fiddle/philly-traces |
| **Alibaba PAI** | NSDI'20 釋出 | 2 個月、6500 GPU、120 萬 tasks | https://github.com/alibaba/clusterdata |
| **Helios** | SC'21 (港中文) | 6 月、4 cluster、超過 70 萬 jobs | https://github.com/S-Lab-System-Group/HeliosData |
| **MLaaS Alibaba** | NSDI'22 | 含推論 + 訓練 | clusterdata 子集 |
| **Acme (LLM 訓練)** | NSDI'24 | 大模型訓練 trace | 詢問作者 |

對你的場景，**Philly 或 Helios 最適合** — Alibaba 規模太大（要 sub-sample），Acme 是 LLM 專用。

### 7.5 哪種演算法最適合排程 RL？

如果決定走 §7.3 第三階段：

| 演算法 | 優點 | 缺點 | 適合場景 |
|--------|------|------|----------|
| **REINFORCE / VPG** | 簡單，好 debug | 高 variance | 玩具實驗 |
| **PPO** | 穩定、業界主流 | 需 advantage estimation | **首選** |
| **DQN / Rainbow** | 離散 action 經典 | continuous action 不行 | action 是離散 job 選擇時 |
| **Soft Actor-Critic (SAC)** | continuous action 強 | 複雜 | score weight 連續調整 |
| **Contextual Bandit (LinUCB)** | 有 regret bound、簡單 | 不能多步規劃 | weight tuning |

**RLScheduler (SC'20) 用 PPO + 自訂 kernel-based policy network**，可以當 reference implementation。

---

## 8. 自訂 Slurm Priority Plugin — 具體能做什麼

> §5.3 提過這條路，這節展開「具體寫什麼 code、加什麼 feature、跟現有系統怎麼接」。

### 8.1 Slurm 的 plugin 架構

Slurm 是 plugin 化的，大部分排程行為都可以替換：

```
src/plugins/
├── priority/         ← job 在 queue 的優先序怎麼算
│   ├── basic/        ← FIFO（我們現在用的）
│   ├── multifactor/  ← 加權多因子（age + fairshare + qos + size + ...）
│   └── <custom>/     ← 你寫的
├── select/           ← job 該放在哪些 node
│   ├── linear/
│   └── cons_tres/    ← 我們現在用
├── sched/            ← 主排程迴圈
│   ├── builtin/
│   └── backfill/     ← 我們現在用
├── gres/             ← 非 CPU 資源（GPU、MPS、license）
│   ├── gpu/
│   └── mps/
└── preempt/
```

每個 plugin 是一個 `.so`，照固定 ABI（一組 `extern "C"` 函式）寫 C code，build 進 `/usr/lib/x86_64-linux-gnu/slurm-wlm/`。

### 8.2 對我們場景，custom priority plugin 能加哪些 factor

`priority/multifactor` 預設算式：

```
priority = PriorityWeightAge * f_age
         + PriorityWeightFairshare * f_fairshare
         + PriorityWeightJobSize * f_size
         + PriorityWeightPartition * f_partition
         + PriorityWeightQOS * f_qos
         + Σ PriorityWeightTRES[t] * f_tres[t]
```

你可以 fork 這個 plugin（Slurm `src/plugins/priority/multifactor/priority_multifactor.c`），加新 factor：

| 新 factor | 公式（草稿） | 用什麼資料源 |
|-----------|---------------|---------------|
| `f_mps_fit` | `1 - (mps_request_slots / total_free_mps_slots)` | slurmctld 內部 GRES table |
| `f_vram_fit` | `min(1, vram_avail / vram_request)` | gres.conf 加 `Type=rtx4070,VRAM=12288`（Slurm 不原生支援，要 hack） |
| `f_topology` | `1 / (1 + nccl_hops_to_peers)` | sbatch `--switches=`（Slurm 內建）或自訂 feature label |
| `f_predicted_runtime` | `1 - sigmoid(predicted_seconds / 3600)` | 呼叫 §9 的 runtime predictor |
| `f_checkpoint_freshness` | `exp(-age_seconds / 600)` | Operator 已經在追蹤的 checkpoint mtime |

### 8.3 不寫 C 也能達到 80% 效果：Submit Plugin (Lua)

Slurm 提供 **Lua submit plugin**（不用 C、不用編譯）：

```ini
# slurm.conf
JobSubmitPlugins=lua
```

```lua
-- /etc/slurm/job_submit.lua
function slurm_job_submit(job_desc, part_list, submit_uid)
    -- 從外部 service 拉預測的 runtime
    local predicted = http_get("http://runtime-predictor:8080/predict?script=" .. job_desc.script_hash)
    if predicted then
        job_desc.time_limit = math.ceil(predicted / 60)  -- 分鐘
    end

    -- 根據 GRES 自動調整 nice / priority
    if job_desc.gres == "mps:25" then
        job_desc.priority = job_desc.priority + 100  -- 小 job 優先 backfill
    end

    return slurm.SUCCESS
end
```

**對 Phase 6 來說，這是最高 CP 值的入口**：

- 不動 Slurm source code，不影響升級路徑
- 可以 hot-reload（改 .lua 不用重啟 slurmctld）
- 可以呼叫外部 HTTP service（接 §9 的 ML predictor）

### 8.4 進階：寫 sched/ plugin 改 backfill 邏輯

這是「真正的排程改造」，但成本最高：

- Fork `src/plugins/sched/backfill/backfill.c`
- 改 `_attempt_backfill()` 函式 — 在它決定 job 放到哪 node 之前，插入你的 score function
- 改 reservation 邏輯 — 例如「為高優先 job 預留未來 X 分鐘的 GPU0」

**只有在第一階段（公式 + Lua plugin）撞牆後才推薦做**。

### 8.5 Phase 6 chart 整合計畫

```yaml
# chart/values.yaml 新增
slurm:
  jobSubmit:
    plugin: lua
    luaScript: |
      -- inline 或從外部 file
  customPriority:
    enabled: false  # 預設關，避免影響 helm-unittest
    factorWeights:
      mpsFit: 100
      vramFit: 50
      topology: 200
      predictedRuntime: 300
```

新增 ConfigMap `chart/templates/configmap-job-submit.yaml`，掛到 controller pod 的 `/etc/slurm/job_submit.lua`。

---

## 9. ML-based Runtime Prediction — 具體做法與系統整合

> 這條路是 **「用 ML，但不是用 ML 做排程決策本身」**。它的角色是給 Slurm backfill 一個更準的 `--time` 估計，讓 backfill 演算法可以更積極地塞小 job。產線（NERSC、ALCF、Microsoft）真的會這樣用。

### 9.1 為什麼這個有用

`sched/backfill` 演算法核心邏輯：「在不延誤前面 job 開始時間的前提下，挑 queue 後面的 job 提早跑」。它需要知道**每個 job 還會跑多久**才能算「會不會延誤」。

預設來源：使用者填的 `--time`（wall time limit）。但使用者填的 wall time 通常**估太大**（怕 job 被殺，習慣填 24h）。Slurm 把 24h 當 worst case 來規劃 backfill，結果該插隊的小 job 沒插進去。

如果有準確的 runtime 預測（例如「這個 job 通常跑 35 分鐘」），backfill 就能正確判斷「這個 9 分鐘的 small job 可以塞進來」。

**已知的成果**：NERSC 在 Cori 用 GBDT 預測 runtime，backfill 利用率提升 5–15%（視 workload 而定）。

### 9.2 模型設計

**任務**：給定 job 的提交資訊，預測實際 wall time 秒數。

**Features**（從 `sacct` 或 sbatch script 抽）：

| Feature | 來源 | 為什麼有用 |
|---------|------|------------|
| 使用者 ID / 帳號 | sbatch | 每個人 workload 模式很穩定 |
| sbatch script content hash 或 normalized AST | 自己解析 | 同樣的腳本 runtime 高度相似 |
| Partition / GRES / cpus / mem | sbatch | 資源越多通常越快但不線性 |
| Job array index（如果有） | sbatch | array 的每個 task 通常很像 |
| 時段（hour-of-week） | submit time | 系統負載不同 |
| 過去 N 次同腳本的 runtime 統計 | sacct | 最強 feature，bias-variance |
| Dataset 大小（如果可從 args 抽） | parse `--input` 等 | 可選 |

**模型**：

| 選項 | 優點 | 缺點 |
|------|------|------|
| **GBDT (LightGBM/XGBoost)** ✅ 推薦 | 強、快、好解釋、不用 GPU 訓練 | 表格資料天花板 |
| Linear regression | 解釋性極強 | 太簡單 |
| Neural network (MLP) | 彈性高 | 過擬合風險、訓練成本 |
| **3Sigma**（distribution-based, EuroSys'18） | 預測整個分布而非點估計，給 backfill 用更好 | 實作複雜 |

**目標函式**：log-runtime 的 MAE（runtime 跨好幾個量級，必須 log scale）。

```python
# 概念性訓練流程（10 行 LightGBM）
import lightgbm as lgb
df = pd.read_csv("sacct_dump.csv")
df["log_runtime"] = np.log1p(df["elapsed_seconds"])
X = df[features]; y = df["log_runtime"]
model = lgb.LGBMRegressor(n_estimators=500, num_leaves=63)
model.fit(X_train, y_train, eval_set=[(X_val, y_val)], early_stopping_rounds=20)
```

### 9.3 跟現有系統的整合（Phase 6 / Phase 7 都用得到）

```
                       ┌─────────────────────────┐
                       │  runtime-predictor      │
                       │  (FastAPI + LightGBM)   │  ← chart/templates/runtime-predictor/
                       │  /predict (POST)        │
                       │  /retrain (POST, cron)  │
                       └────────┬────────────────┘
                                │ HTTP
        ┌───────────────────────┼─────────────────────────┐
        │                       │                         │
        ▼                       ▼                         ▼
  Login pod              slurmctld                    Operator
  (sbatch wrapper)       (Lua submit plugin)          (Phase 7 OTel: 把預測 vs 實際寫進 trace)
        │                       │
        └────► 設 --time ◄──────┘
                  │
                  ▼
            sched/backfill 用更準的 --time 做規劃
```

**具體新增的 chart 元件**：

| 檔案 | 內容 |
|------|------|
| `chart/templates/runtime-predictor-deployment.yaml` | FastAPI + LightGBM service |
| `chart/templates/runtime-predictor-cronjob.yaml` | 每週從 sacct 抽 trace、retrain、寫回 PVC |
| `chart/templates/runtime-predictor-pvc.yaml` | 存 model artifact + 訓練資料 |
| `chart/templates/configmap-job-submit.yaml` | Lua plugin 呼叫 predictor |
| `chart/values.yaml` 新增 `runtimePredictor.enabled / image / model` 段 |

**訓練資料怎麼來**：

1. `sacctmgr show events`、`sacct --format=JobID,User,Partition,ReqGRES,Elapsed,Start,End,...`
2. 已經有 slurmdbd + MySQL（Phase 1 完成）— 直接 query MySQL
3. CronJob 每週把新資料 dump 出來，retrain，atomic rename model file

**啟動冷啟動問題**：剛部署時沒歷史資料 — 兩條路：

- **Bootstrap with prior**：用使用者填的 `--time` 當預測值（fallback），收集 ≥ 100 個 job 後切換
- **Transfer learning**：拿公開 trace（Philly / Helios）pretrain，再 fine-tune

### 9.4 跟 thesis 怎麼結合

§7.3 第一階段 score function 的 `f_predicted_runtime` factor 就是這節的 output。也就是：

```
score(job, placement) = ... + ε · f_predicted_runtime(job)
```

`f_predicted_runtime` 由本節 service 提供，不是手填的。**這讓你的 thesis 第一階段（公式）和 ML 部分（runtime prediction）天然接起來** — predictor 的 MAE 是一個獨立可量化的指標、score function 的有效性是另一個獨立可量化的指標、兩者組合的端到端效果（JCT、utilization）是第三個可量化的指標。三層都有數字、好寫 evaluation 章節。

### 9.5 評估指標

| 層 | 指標 | 怎麼測 |
|----|------|---------|
| Predictor 本身 | MAE / MAPE on log-runtime | sacct hold-out set |
| Backfill 改善 | Backfill scheduling rate（成功 backfill 的 job 比例） | Slurm metric `bf_*` |
| 端到端 | JCT p50/p95、cluster utilization、makespan | trace replay 或實機 |
| 公平性 | Job slowdown variance、min-max ratio | 同上 |

---

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

### 公式化 / Analytical 排程（§7.1）
- **Themis**: Mahajan et al., "Themis: Fair and Efficient GPU Cluster Scheduling", NSDI 2020
- **Optimus**: Peng et al., "Optimus: An Efficient Dynamic Resource Scheduler for Deep Learning Clusters", EuroSys 2018
- **AFS**: Hwang et al., "Elastic Resource Sharing for Distributed Deep Learning", NSDI 2021
- **Synergy**: Mohan et al., "Looking Beyond GPUs for DNN Scheduling on Multi-Tenant Clusters", OSDI 2022
- **3Sigma**: Park et al., "3Sigma: distribution-based cluster scheduling for runtime uncertainty", EuroSys 2018

### DRL 排程相關（§7.2、§7.5）
- **Harmony**: Bao et al., "Online Job Scheduling in Distributed Machine Learning Clusters", INFOCOM 2018 / SoCC 2019
- **DL2**: Peng et al., "DL2: A Deep Learning-Driven Scheduler for Deep Learning Clusters", IEEE TPDS 2021
- **RLScheduler**: Zhang et al., "RLScheduler: An Automated HPC Batch Job Scheduler Using Reinforcement Learning", SC 2020
- **Horus**: Yeung et al., "Horus: Interference-Aware and Prediction-Based Scheduling in Deep Learning Systems", IEEE TPDS 2021
- **Lucid**: Hu et al., "Lucid: A Non-intrusive, Scalable and Interpretable Scheduler for Deep Learning Training Jobs", ASPLOS 2023
- **GPARS**: GPU Scheduling for LLM Serving, MLSys 2024

### Public GPU traces（§7.4）
- Philly trace — https://github.com/msr-fiddle/philly-traces
- Alibaba PAI trace — https://github.com/alibaba/clusterdata
- Helios trace — https://github.com/S-Lab-System-Group/HeliosData
- MLaaS Alibaba — Weng et al., "MLaaS in the Wild", NSDI 2022

### Slurm plugin 開發（§8）
- Slurm Plugin API — https://slurm.schedmd.com/plugins.html
- Job Submit Plugin — https://slurm.schedmd.com/job_submit_plugins.html
- Job Submit Lua — https://github.com/SchedMD/slurm/blob/master/contribs/lua/job_submit.lua

### ML runtime prediction（§9）
- Allcock et al., "Experiences and Lessons Learned from a Vibrant HPC Job Scheduler Trace Database" (ALCF runtime prediction)
- Tanash et al., "Improving HPC System Performance by Predicting Job Resources via Supervised Machine Learning", PEARC 2019
- Park et al., "3Sigma" (上面已列)

### Google / Microsoft 產線
- Verma et al., "Large-scale cluster management at Google with Borg", EuroSys 2015
- Tirmazi et al., "Borg: the next generation", EuroSys 2020
- Microsoft Singularity — https://arxiv.org/abs/2202.07848
