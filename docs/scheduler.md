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

---

# Phase 6 Roadmap — Score-based Scheduling 開發追蹤（R17）

> **Owner**: 你（單人專題）
> **起手日**: 2026-05-06（R21 event-driven operator 完成後立刻可起）
> **預計總工期**: 7–10 週（必做 M1–M8 ≈ 7–8 週、含 ★ M9 ≈ 10 週）
> **依賴**: R5（DCGM）✅、R7（resources/limits）✅、R21（event-driven loop）✅
> **review.md 對應**: R17（score-based scheduling）+ R19（runtime predictor）

把 §5–9 拆成可勾選的 milestone。每個 milestone 列：**目標、產出檔案、驗收條件、預估工期**。順序按依賴 + 工程成本遞增排，前一個沒做下一個沒法做。

## 進度總覽

| # | Milestone | 對應節 | 工期 | 狀態 |
|---|---|---|:---:|:---:|
| M1 | Slurm 內建調度旋鈕（chart values） | §1, §5.1 | 2 天 | ✅ |
| M2 | Score function 規格 + Lua submit plugin scaffold | §7.1, §8.3 | 3 天 | ⬜ |
| M3 | Score v1：mps_fit + vram_fit + fragmentation_penalty | §7.1, §8.2 | 5 天 | ⬜ |
| M4 | Trace replay simulator（Philly subsample） | §7.4 | 5 天 | ⬜ |
| M5 | Runtime predictor service（FastAPI + LightGBM） | §9 | 7 天 | ⬜ |
| M6 | Predictor → Lua → backfill 端到端 | §9.3 | 3 天 | ⬜ |
| M7 | Fragmentation detector + 自動 requeue（Gandiva-lite） | §5.2 | 5 天 | ⬜ |
| M8 | Evaluation：JCT / utilization / makespan / fairness | §9.5 | 7 天 | ⬜ |
| M9 ★ | （可選）Contextual bandit / PPO weight tuning | §7.5 | 10 天 | ⬜ |

★ = 加分項，做不完不影響畢業/上線。

## M1：Slurm 內建調度旋鈕（2 天）

### 目標
把 §5.1 的 ini 設定走 chart values，確認對既有 workload 沒有 regress。**不寫程式就能拿到的免費收益**，也是後面所有 milestone 的 baseline。

### 產出檔案
- `chart/values.yaml` — 新 `slurm.scheduling.{selectTypeParameters, schedulerParameters, priorityWeights, preempt}` 段
- `chart/templates/_helpers.tpl` — `slurm-platform.slurmConf` helper render 進 slurm.conf
- `chart/templates/configmap-static.yaml` — 透過 helper 自動帶入
- `chart/tests/slurm_conf_test.yaml` — 至少 3 條：default 不變、`CR_Pack_Nodes` 開時 SelectTypeParameters 對、preempt 預設關
- `docs/scheduler.md` — 在 §5.1 加 ✅ + 實機觀察的 backfill 命中率

### 預設值（建議）
```yaml
slurm:
  scheduling:
    selectTypeParameters: "CR_Core,CR_Pack_Nodes"
    schedulerParameters: "bf_window=720,bf_resolution=30,bf_continue,bf_max_job_test=200"
    priorityWeights:
      age: 1000
      fairshare: 0
      jobSize: 500
      partition: 1000
      qos: 2000
    preempt:
      enabled: false      # 等 M7 才打開（要 application 配合 checkpoint）
      type: preempt/qos
      mode: REQUEUE
```

### 驗收條件
- [ ] `helm-unittest` 全綠（含 3 條新測試）
- [ ] `verify.sh` 全綠 — 既有 workload 無 regress
- [ ] sbatch 5 個 cpu job，sinfo 觀察 backfill 命中率 ≥ 0（沒 worse than baseline）
- [ ] `scontrol show config | grep -E "SchedulerParameters|SelectTypeParameters|PriorityWeight"` 對得上 values

### 風險
- `CR_Pack_Nodes` 在某些 partition 配置下會讓 fragmentation 暫時更糟（先 pack 滿一台才開下一台）— evaluation 時要量測 baseline vs M1 的 utilization 比例

### 實機驗收（2026-05-06，custom-sched）

- 5 條 helm-unittest 全綠（67/67 total）；`SchedulerType=sched/backfill` / `SchedulerParameters=bf_window=720,...` / `PriorityType=priority/multifactor` / `PriorityWeight{Age,JobSize,Partition,QOS}` 全部從 `scontrol show config` 回讀對齊 values
- `verify.sh` baseline 全綠（單機 srun + sbatch / scale-up→4 / scale-down→1 / PMI2 / OpenMPI / GPU pool）
- 5 條 cpu sleep job：4 條同時跑在 cpu-0 / cpu-1，第 5 條 PENDING(Resources)，operator scale-up 觸發後排空 → backfill + 既有 elastic loop 沒互打架
- `CR_Pack_Nodes` 暫不打開（roadmap 預設仍 `CR_Core`），等 M3 fragmentation_penalty 上線後再做 baseline vs pack 的 A/B
- preempt 維持 `enabled: false`，slurm.conf 不出 `PreemptType` / `PreemptMode`；M7 才會翻

## M2：Score function 規格 + Lua submit plugin scaffold（3 天）

### 目標
- 把 §7.1 的 score 公式寫成可執行規格（**規格不是 code**）
- 接 Slurm Lua submit plugin，**先什麼都不算**，只證明 `slurmctld` 能呼叫 lua、能讀 job_desc 各欄位、能寫回 priority/time_limit
- 預留呼叫外部 HTTP service 的 hook（M6 用）

### 產出檔案
- `docs/scheduler-score-spec.md`（新）— 公式、變數定義、係數初值、I/O schema、單元測試 case
- `chart/templates/configmap-job-submit.yaml`（新）— ConfigMap 包 `job_submit.lua`
- `chart/templates/controller.yaml` — 新 mount `/etc/slurm/job_submit.lua`，slurm.conf 加 `JobSubmitPlugins=lua`
- `chart/values.yaml` — `slurm.jobSubmit.{enabled, lua}`（預設 enabled=false 直到 M3）
- `chart/tests/job_submit_test.yaml`（新）— 5 條
- `scripts/verify-lua-submit.sh`（新）— `lua -e 'dofile(...)'` syntax check + 進 controller pod 看 plugin 加載 log

### Score function 規格雛形
```
score(J, P) = α·f_mps_fit(J,P)        ∈ [0,1]   MPS slot 容量配適
            + β·f_vram_fit(J,P)       ∈ [0,1]   VRAM 餘裕
            + γ·f_topology(J,P)       ∈ [0,1]   NCCL 拓撲親和性
            - δ·f_fragmentation(J,P)  ∈ [0,1]   放完後碎片化代價
            + ε·f_pred_runtime(J)     ∈ [0,1]   預測 runtime（M5 之後加）

初值（一階段手調）: α=0.40, β=0.20, γ=0.15, δ=0.20, ε=0.05
（變更係數要動 sensitivity analysis，記在 spec 文件裡）
```

### 驗收條件
- [ ] spec 文件含每個 factor 的 input、output、邊界 case 表
- [ ] `helm-unittest` 60+ 條全綠
- [ ] 進 controller pod `tail -f /var/log/slurm/slurmctld.log`，sbatch 一個 job 看到 lua plugin invoke 成功
- [ ] lua 改一個值（e.g. 硬寫 `job_desc.priority = 9999`），sbatch 後 squeue 看到該 priority

## M3：Score v1 — mps_fit + vram_fit + fragmentation（5 天）

### 目標
把 M2 規格中前 4 個 factor（除 `f_pred_runtime`）真的算出來。**先單機 + 假資料過 unit test，再上真機驗證。**

### 產出檔案
- `chart/scripts/job_submit.lua`（從 ConfigMap 移到實體檔，方便 lint / test）
  - `f_mps_fit`：解析 `job_desc.tres_per_node`（`gpu:rtx4070:1,mps:25`），對 sinfo 拿到的 free MPS slot 比
  - `f_vram_fit`：簡化版 — `node_features` 含 `vram-12g` / `vram-24g` 標籤，job 用 `--constraint=vram-12g+`
  - `f_fragmentation`：放完後 GPU MPS slot 餘裕 stddev（越大越碎）
- `tests/lua/score_test.lua`（新，`busted`）— 10–15 case 測每個 factor 邊界
- `scripts/render-score-trace.py`（新）— sbatch + score 過程序列化成 JSONL，方便 M8 evaluation
- `chart/dashboards/scheduler.json`（新 panel）— `slurm_lua_score{factor=...}` histogram、`slurm_score_decision_total`

### 驗收條件
- [ ] `busted` lua unit test 全綠
- [ ] sbatch 5 個 mix（不同 mps、不同 vram constraint），squeue 順序與 score 排序一致
- [ ] Grafana panel score histogram，p50 在 [0.3, 0.7]
- [ ] `verify.sh` 沒 regress

### 風險
- Lua 沒 native MPS 計算 API — 從 sinfo / scontrol show node 字串解析。寫個小工具函式 + cache 1 秒避免 hot path 卡 slurmctld

## M4：Trace replay simulator（5 天）

### 目標
真機規模太小（2 GPU、3 pool），有統計意義的比較必須走 trace replay。挑 **Philly trace**（~400 GPU、3000 jobs，2 個月）。

### 產出檔案
- `sim/`（新目錄）
  - `sim/loader.py` — 讀 Philly tar.gz，正規化成 `(job_id, user, gpu_count, gpu_type, submit_ts, runtime, mem_req)`
  - `sim/cluster.py` — 模擬 cluster：N 個 GPU、可掛 MPS、track free slots
  - `sim/scheduler/{fcfs,multifactor,score}.py` — 三個 baseline + 我們的 score
  - `sim/runner.py` — 跑 trace、收集 metric、輸出 CSV
  - `sim/metrics.py` — JCT / makespan / utilization / slowdown
- `sim/tests/` — pytest，至少 3 個 sanity test
- `docs/sim-readme.md` — 怎麼下 trace、怎麼跑

### 驗收條件
- [ ] 1000-job subsample of Philly，FCFS 跑完輸出 metric CSV
- [ ] 同一 trace 跑 FCFS vs multifactor vs score(M3 版)，三者 metric 都有合理數字
- [ ] `sim/runner.py --scheduler score --trace philly_subsample.json` < 60 秒跑完

### 風險
- Philly trace 沒 MPS 資訊（純整 GPU）— augment 假設「每張 GPU 4 個 MPS slot」，evaluation 章節要說明

## M5：Runtime predictor service（7 天）

### 目標
§9 — 給 backfill 一個比 user `--time` 準的 runtime 預測。**先做 service + 訓練 pipeline，暫時不接 lua（M6 才接）**。

### 產出檔案
- `services/runtime-predictor/`（新）
  - `app.py` — FastAPI，`/predict` `/retrain` `/healthz`
  - `train.py` — LightGBM regressor，log-runtime MAE，hold-out CV
  - `features.py` — 抽 user / partition / gres / cpus / hour-of-week / past-N-runs
  - `requirements.txt` — fastapi, lightgbm, scikit-learn, pandas, sqlalchemy, prometheus-client
  - `Dockerfile`, `tests/`
- `chart/templates/runtime-predictor/`（新）
  - `deployment.yaml` — Service + Deployment + PVC
  - `cronjob.yaml` — 每週日 03:00 retrain
  - `network-policy.yaml` — 允許 controller pod → predictor:8080
- `chart/values.yaml` — `runtimePredictor.{enabled, image, schedule, modelPvc}`
- `chart/tests/runtime_predictor_test.yaml` — 8 條
- `scripts/verify-runtime-predictor.sh`

### 驗收條件
- [ ] `train.py` 拿 Philly subsample 訓練，hold-out MAE on log(runtime+1) < 1.0
- [ ] FastAPI `/predict` p95 < 50ms
- [ ] CronJob 跑成功，model artifact 被替換、舊 model 保留 1 份備份
- [ ] `verify-runtime-predictor.sh` 全綠

### 風險
- Cold start：bootstrap_with_prior，預設回 `min(user_time_limit, 4*3600)` 直到累積 ≥ 100 sample
- sacct 抽資料連 MySQL — chart 加 read-only ServiceAccount

## M6：Predictor → Lua → backfill 端到端（3 天）

### 目標
把 M5 service 接到 M2 lua plugin。Lua sbatch 時呼叫 predictor，把回傳 seconds 寫成 `job_desc.time_limit`（user 沒填或填得超大時才覆蓋）。

### 產出檔案
- `chart/scripts/job_submit.lua` — 加 HTTP call（`socket.http` + `lua-cjson`）
- `chart/templates/configmap-job-submit.yaml` — 注入 `predictor_url`
- `chart/values.yaml` — `slurm.jobSubmit.predictor.{enabled, url, timeoutMs, fallbackHours}`
- `chart/tests/job_submit_test.yaml` — 加 2 條

### 驗收條件
- [ ] sbatch 一個 job，controller log 確認 lua 拿到預測值 + 寫進 time_limit
- [ ] `bf_*` Slurm metric 顯示 backfill scheduling rate 上升（vs M1 baseline）
- [ ] predictor 掛掉時 lua fallback 不影響 sbatch（`pcall` 包 HTTP call）

### 風險
- slurmctld lua plugin 是 in-process call，HTTP timeout 卡住整個排程器 — 必須設 `timeoutMs=200` 上限

## M7：Fragmentation detector + 自動 requeue（5 天）

### 目標
§5.2 — Operator 加「fragmentation 偵測 + 主動 requeue」迴圈。發現 pending high-prio job 被卡住 + 有可被 kill 的 low-prio job 能解卡時，主動 `scontrol requeue`，由 sbatch wrapper 處理 resume。

### 產出檔案
- `operator/fragmentation.py`（新）
  - `class FragmentationDetector` — 每 5s 從 collector 拿 squeue + sinfo，算 free GPU slot 分布、pending 需求、可解卡 low-prio set
  - `class RequeueDecider` — fragmentation snapshot → 「kill 哪幾個 low-prio」
- `operator/app.py` — 加新 reconcile source `fragmentation`，event-driven 觸發（R21 framework 已有）
- `operator/metrics.py` — `slurm_operator_fragmentation_score` gauge、`slurm_operator_requeue_total{reason}` counter
- `chart/templates/configmap-task-prolog.yaml` — sbatch wrapper 加 `resume_from_checkpoint()`
- `chart/values.yaml` — `operator.fragmentation.{enabled, minIntervalSeconds, maxRequeuesPerHour}`
- `chart/tests/operator_test.yaml` — 新 env 驗證

### 驗收條件
- [ ] 模擬 fragmentation：4 個 mps:25 占滿 → 1 個 mps:50（pending）→ operator 偵測 + requeue 1 個 mps:25 → 大 job 開始跑（log 看得到 `requeue_decision`）
- [ ] requeued job 自己 resume，loss 從 ckpt 接續（DDP MNIST or 小 LLaMA toy）
- [ ] rate-limit 生效：強制觸發 6 次/小時，第 6 次 reject + warn
- [ ] `verify.sh` 全綠

### 風險
- 「以為能解卡，requeue 完 GPU 被別 job 搶走」— mitigation：requeue 後 5 秒內若 pending 還沒 start，回滾 + log
- ckpt resume 依賴 user code — 提供 reference template，evaluation 用 reference workload 跑

## M8：Evaluation（7 天）

### 目標
產出 thesis evaluation 章節的所有圖表 + 數字。**不寫新 code，純跑實驗 + 出圖**。

### 產出檔案
- `eval/scripts/run_all.sh` — 跑齊 baseline + 我們的版本
- `eval/results/` — 每組實驗 raw CSV
- `eval/figures/` — matplotlib PNG + PDF
- `docs/eval-writeup.md` — 圖 + 結論寫成 thesis 草稿

### 實驗矩陣
| 實驗 | scheduler | trace | 主要指標 | 預期結論 |
|---|---|---|---|---|
| E1 baseline | FCFS | Philly 1k | JCT, util | 上界（worst） |
| E2 vendor | multifactor | Philly 1k | 同上 | Slurm 預設 |
| E3 our v0 | M3 score（無 predictor） | Philly 1k | 同上 | 改善多少 |
| E4 our v1 | M3 score + M5 predictor | Philly 1k | 同上 + bf_rate | predictor 邊際價值 |
| E5 our v2 | E4 + M7 fragmentation | Philly 1k | 同上 + 解卡次數 | requeue 邊際價值 |
| E6 sensitivity | E4 跑 9 組 (α,β,γ,δ) | Philly 1k | JCT 等 | 證明係數可調 |
| E7 真機 | E4 vs E2 | 自製 50-job mix | wall-clock JCT | sim → 真機可重現 |

### 驗收條件
- [ ] 7 組實驗 raw data 齊全
- [ ] 7 張圖出爐（CDF of JCT、box of slowdown、line of utilization over time）
- [ ] eval-writeup.md 約 8–12 頁、每張圖 1 段論述

### 風險
- 結論不顯著（score ≈ multifactor）— M3 / M6 之後就先跑 mini-eval 抓問題，不要拖到 M8 才發現

## M9 ★（可選）：Bandit / PPO weight tuning（10 天）

### 目標
§7.3 第三階段。把 score function 的 (α, β, γ, δ, ε) 當 RL action，state = cluster snapshot，reward = -JCT。

### 產出檔案
- `services/weight-tuner/`（新）
  - `bandit.py` — LinUCB / EXP3
  - `ppo.py` — Stable-Baselines3 PPO + 自製 gym env（包 `sim/`）
  - `tests/`
- `chart/templates/weight-tuner/` — Service + PVC（policy artifact）
- `chart/values.yaml` — `weightTuner.enabled` 預設關
- 接到 Lua：lua 從 weight-tuner pull 最新 weight（每 60s cache）

### 驗收條件
- [ ] PPO 在 sim 上 1000 episode 後，eval JCT 比 fixed weight 改善 > 5%（如果沒，記錄 negative result，仍是論文章節）
- [ ] 在線（真機）4 hours，policy lag 不超過 60s
- [ ] 不影響既有 acceptance criteria（爆掉時自動 fallback fixed weight）

## 排程依賴圖

```
M1 ──┐
     ├──► M2 ──► M3 ──┬──► M4 ──┬──► M8
     │                 │         │
     │                 └──► M5 ──┼──► M6 ──┘
     │                           │
     │                           └──► M7 ──┘
     │                                     │
     └─────────────────────────────────────► M9 (★)
```

- M1 是先決條件（沒它後面 baseline 不對）
- M2/M3/M4/M5 可並行（不同檔案）
- M6 / M7 都吃 M3 + 自己的依賴（M5 / 無）
- M8 在 M3 + M5 + M6 + M7 完成後跑

## 與 Phase 7（R16 OTel）的關係

Phase 7 OTel 端到端 trace 會把每個 job 的：
1. submit (login pod) → submitted (slurmctld)
2. submitted → priority computed (lua + score function 回傳)
3. priority computed → scheduled to node (backfill 決定)
4. scheduled → first epoch (worker)
5. first epoch → checkpoint
6. checkpoint → completion / requeue (M7 觸發)

每個 step span 內會帶上 score 各 factor 的值、predictor MAE、fragmentation snapshot。**Phase 6 寫 trace span 的成本接近 0**（lua emit log line + operator emit metric 已經有了），等 Phase 7 起手時把這些 log/metric 包成 OTel span 即可。

## 維護準則

1. **每完成一個 milestone 就更新本節進度總覽**（⬜ → ✅，註記 commit hash）
2. **每個 milestone 最後 5% 是寫測試 + 文件**，不要跳過 — Phase 6 evaluation 章節要靠這些當「reproducibility appendix」
3. **不要先做 M9**。M9 RL novelty 只在 M1–M8 跑出 baseline 數字後才有意義
4. **scheduler.md §1–9 是研究筆記**（不會再大改），**這節 Phase 6 Roadmap 是執行清單**（每天會勾）— 兩個分工不要混

## 起手第一步

```bash
git checkout -b custom-sched
# 1. chart/values.yaml 把 §5.1 的 ini 全部 expose 成 yaml
# 2. helper / configmap 接好
# 3. helm-unittest 加 3 條
# 4. helm upgrade + verify.sh，確認沒 regress
# 5. commit "Phase 6 M1: expose Slurm scheduling knobs in chart values"
```

完成 M1 後回來把這節進度總覽勾掉，附上 commit hash。
