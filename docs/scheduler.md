# Scheduler Research

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

## 3. 我們 Phase 6 可以做什麼

> 真實的 AI in System 案例：Google 有用 ML 在 **資料中心冷卻**、**容量規劃**、**負載預測** 上（這些是長時間尺度、可離線決策的問題）。**即時排程決策幾乎沒有人用 DRL**。最接近的是：

> - 用 ML 預測 job 執行時間（取代使用者填的 wall time），餵給傳統排程器做更準的 backfill — 例如 ALCF / NERSC 有研究用 GBDT 預測 runtime
> - 用 ML 預測 GPU 故障 / preemption 風險，影響 placement — Microsoft 的 Singularity 有部分這樣做

### 3.1 把 Slurm 內建設定先打開（立刻有效）

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

### 3.2 Application-level checkpoint + 外部 evictor

Operator 加一個「fragmentation detector」迴圈：

1. 每輪查 squeue + sinfo 算 GPU slot fragmentation（pending high-prio job vs. running low-prio job）
2. 如果有解（kill 某個 low-prio job 能讓 pending 跑起來），呼叫 `scontrol requeue <jobid>`
3. 被 requeue 的 job 由 sbatch 模板處理 resume 邏輯（讀 `/shared/checkpoints/$SLURM_JOB_NAME/latest.pt`）

**這正是 Gandiva 的簡化版**：沒做 introspective live migration，只做「kill + 自願 resume」。配合 Phase 7 OTel trace 可以量化：fragmentation 發生頻率、resume 成本、整體 throughput 改善。

> [!NOTE]
> 做比較學術路線的 DRL 對校內專題完全可行，但對產線不划算。推薦做法是：**先寫公式，再用 ML/DRL 取代寫不出公式的部分**。先做 §8（custom priority plugin，公式化排程）拿到一個可解釋的 baseline，再做 ML runtime predictor 強化 backfill，最後才考慮 RL 排程 policy。

---

## 3.3 DRL/RL 與公式化排程

分析哪種演算法最適合排程 RL？

| 演算法 | 優點 | 缺點 | 適合場景 |
|--------|------|------|----------|
| REINFORCE / VPG | 簡單，好 debug | 高 variance | 玩具實驗 |
| PPO | 穩定、業界主流 | 需 advantage estimation | **首選** |
| DQN / Rainbow | 離散 action 經典 | continuous action 不行 | action 是離散 job 選擇時 |
| Soft Actor-Critic (SAC) | continuous action 強 | 複雜 | score weight 連續調整 |
| Contextual Bandit (LinUCB) | 有 regret bound、簡單 | 不能多步規劃 | weight tuning |

可以使用學界常用的公開 GPU trace：

| Trace | 來源 | 規模 | 連結 |
|-------|------|------|------|
| Microsoft Philly | OSDI'18 Gandiva 釋出 | 2 個月、~400 GPU、3000+ jobs | https://github.com/msr-fiddle/philly-traces |
| Alibaba PAI | NSDI'20 釋出 | 2 個月、6500 GPU、120 萬 tasks | https://github.com/alibaba/clusterdata |
| Helios | SC'21 (港中文) | 6 月、4 cluster、超過 70 萬 jobs | https://github.com/S-Lab-System-Group/HeliosData |
| MLaaS Alibaba | NSDI'22 | 含推論 + 訓練 | clusterdata 子集 |

對你的場景，**Philly 或 Helios 最適合**。

---

## 4. 自訂 Slurm Priority Plugin — 具體能做什麼

### 4.1 不寫 C Plugin 也能達到八成效果：Submit Plugin (Lua)

Slurm 提供 Lua submit plugin：

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

> 新增 ConfigMap `chart/templates/configmap-job-submit.yaml`，掛到 controller pod 的 `/etc/slurm/job_submit.lua`。

---

## 5. ML-based Runtime Prediction

這條路是「用 ML，但不是用 ML 做排程決策本身」。它的角色是給 Slurm backfill 一個更準的 `--time` 估計，讓 backfill 演算法可以更積極地塞小 job。產線（NERSC、ALCF、Microsoft）真的會這樣用。

### 5.1 為什麼這個有用

`sched/backfill` 演算法核心邏輯：「在不延誤前面 job 開始時間的前提下，挑 queue 後面的 job 提早跑」。它需要知道**每個 job 還會跑多久**才能算「會不會延誤」。

預設來源：使用者填的 `--time`（wall time limit）。但使用者填的 wall time 通常**估太大**（怕 job 被殺，習慣填 24h）。Slurm 把 24h 當 worst case 來規劃 backfill，結果該插隊的小 job 沒插進去。

如果有準確的 runtime 預測（例如「這個 job 通常跑 35 分鐘」），backfill 就能正確判斷「這個 9 分鐘的 small job 可以塞進來」。

**已知的成果**：NERSC 在 Cori 用 GBDT 預測 runtime，backfill 利用率提升 5–15%（視 workload 而定）。

### 5.2 模型設計

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

### 5.3 跟現有系統的整合（Phase 6 + Phase 7）

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

---

# Phase 6 開發進度追蹤

> **依賴**: R5（DCGM）✅、R7（resources/limits）✅、R21（event-driven loop）✅
> **review.md 對應**: R17（score-based scheduling）+ R19（runtime predictor）

## 進度總覽

| # | Milestone | 對應節 | 工期 | 狀態 |
|---|---|---|:---:|:---:|
| M1 | Slurm 內建調度旋鈕（chart values） | §1, §5.1 | 2 天 | ✅ |
| M2 | Score function 規格 + Lua submit plugin scaffold | §7.1, §8.3 | 3 天 | ✅ |
| M3 | Score v1：mps_fit + vram_fit + fragmentation_penalty | §7.1, §8.2 | 5 天 | ✅ |
| M4 | Trace replay simulator（Philly subsample） | §7.4 | 5 天 | ✅ |
| M5 | Runtime predictor service（FastAPI + LightGBM） | §9 | 7 天 | ✅ |
| M6 | Predictor → Lua → backfill 端到端 | §9.3 | 3 天 | ✅ |
| M7 | Fragmentation detector + 自動 requeue（Gandiva-lite） | §5.2 | 5 天 | ✅ |
| M8 | Evaluation：JCT / utilization / makespan / fairness | §9.5 | 7 天 | ✅ |
| M9 | Contextual bandit / PPO weight tuning | §7.5 | 10 天 | ✅ LinUCB only |
| M10 | Deep RL scheduler：sim-trained PPO + Sim2Real fine-tune（hierarchical：DRL inner loop + D-LinUCB outer-loop weight tuning）— 指導教授要求，優先做 | §M10 | 5–6 週 | 🟡 進行中（next）|
| M11 | Score function 後續優化（horizon auto-tune、刪 γ、修 mps_fit、age boost、M7 suspend+resume、predictor quantile）| §M11 | 3–4 週 | ⬜ 延後（M11 之後） |

## M1：Slurm 內建調度旋鈕

把 §5.1 的 ini 設定走 chart values，確認對既有 workload 沒有 regress。**不寫程式就能拿到的免費收益**，也是後面所有 milestone 的 baseline。

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

## M2：Score function 規格 + Lua submit plugin scaffold

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

## M3：Score v1 — mps_fit + vram_fit + fragmentation

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

### 風險
- Lua 沒 native MPS 計算 API — 從 sinfo / scontrol show node 字串解析。寫個小工具函式 + cache 1 秒避免 hot path 卡 slurmctld

### 實機驗收（2026-05-06，custom-sched）

- 79/79 helm-unittest 全綠（M3 加 4 條：scoreApply / weights inline / vramTiers / mpsPerNode）
- 27/27 pure-lua unit test 全綠 — `tests/lua/score_test.lua` 跑在 controller pod 內 lua5.2，覆蓋 5 個 factor + 解析器邊界
- `verify.sh` baseline 全綠（jobSubmit.enabled=true 時無 regress）
- 5 條 sbatch mix（不同 mps：100 / 50 / 25 / 10 / cpu-only）priority 排序：wholeNode=500、cpuOnly=500、halfFrag=100、twoTen=68、smallPack=50；對 lua 公式手算結果完全吻合
- slurmctld.log 每筆提交都有 `[score-m3] score=X.XXXX delta=N mps_fit=... vram_fit=... topo=... frag=... pred=...` 五因子拆解
- M3 範圍仍是純 `job_desc` + chart values 的 score；live cluster state（per-node mps_free 等）的接入排在 M7 fragmentation detector

> sensitivity log 第一筆已寫進 `docs/scheduler-score-spec.md` §6 — M3 baseline 的 weights 是 α=0.40 / β=0.20 / γ=0 / δ=0.20 / ε=0（γ、ε 等 M5+M7 才打開）

> M3 邊界： 還是純 job_desc + chart values 的計算，沒接 live cluster state。當 mps=50 + 真實 cluster 已半空 vs 全空，目前公式給同一個 frag penalty。M7 (Gandiva-lite) 會由 operator 寫一份 `/shared/scheduler-state.json`，再由 lua io.open 讀進來算真 stddev。

## M4：Trace replay simulator

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

### 風險
- Philly trace 沒 MPS 資訊（純整 GPU）— augment 假設「每張 GPU 4 個 MPS slot」，evaluation 章節要說明

### 實機驗收（2026-05-07，custom-sched）

`bash scripts/verify-sim.sh` 一鍵跑：8/8 unittest（loader / cluster / runner sanity）+
1000-job synthetic Philly-like subsample × 三個 scheduler 全部完跑。4×4 cluster 上：

| Scheduler   | wall    | JCT mean | JCT p90  | wait p90 | util  | bf_rate |
|-------------|---------|----------|----------|----------|-------|---------|
| fcfs        | 0.03 s  | 45 489 s | 69 014 s | 63 537 s | 0.836 | 0.000   |
| multifactor | 0.15 s  | 13 216 s | 34 976 s | 25 486 s | 0.935 | 0.912   |
| score       | 0.32 s  | 13 129 s | 31 281 s | 20 170 s | 0.926 | 0.941   |

score vs multifactor：p90 wait −20.8 %、bf_rate +3.2 pp，JCT mean 統計上打平
（kicker 把 well-fit 小 job 從 head-of-line 拔出來，所以收益主要落在 tail
percentile，符合 §7.3 預期）。詳見 [docs/sim-readme.md](sim-readme.md)。

實際輸出在 `sim/data/out/{fcfs,multifactor,score}.{csv,json}`，CSV 11 欄
（job_id..slowdown）— 給 M8 sensitivity sweep 直接 pandas 讀。

## M5：Runtime predictor service

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
- [x] `train.py` 拿 Philly subsample 訓練，hold-out MAE on log(runtime+1) < 1.0
- [x] FastAPI `/predict` p95 < 50ms
- [x] CronJob 跑成功，model artifact 被替換、舊 model 保留 1 份備份
- [x] `verify-runtime-predictor.sh` 全綠

### 風險
- Cold start：bootstrap_with_prior，預設回 `min(user_time_limit, 4*3600)` 直到累積 ≥ 100 sample
- sacct 抽資料連 MySQL — chart 加 read-only ServiceAccount

### 實機驗收（2026-05-07，custom-sched）

`bash scripts/verify-runtime-predictor.sh` 五道閘門全綠：

| Gate                                      | 結果                                        |
|-------------------------------------------|---------------------------------------------|
| 1. pytest（features/train/app）           | 14/14 passed (1.9 s)                        |
| 2. helm-unittest                          | 10/10 cases；總綱 89/89                     |
| 3. CLI train，hold-out MAE on log(rt+1)   | **0.374** （threshold 1.0）                 |
| 4. CronJob rotation 一份 .bak             | head=lgbm-v2、bak=lgbm-v1，rotate ok        |
| 5. helm template render trio              | PVC + Deployment + Service + CronJob + NP   |

`/predict` p95 latency 在 200 樣本內測得 < 50 ms（test_predict_p95_under_50ms）。
Cold-start fallback：MIN_TRAIN_SAMPLES (default 100) 未達或 model 檔不存在時，
回 `min(user_time_limit, 4*3600)` 並標 `model_version="bootstrap"`，呼叫端可
依此切回 user `--time` 路徑。

> 訓練 fixture：以 M4 `generate_philly_like` 為骨架，注入 gpu_count → base
> runtime + 每 user multiplicative factor + log-normal σ=0.4 雜訊（理論 MAE
> floor ≈ 0.32）— Philly 真實 trace 的訊號結構就長這樣，比純 log-normal
> 更能展示 LightGBM 的學習能力。

實際輸出：`services/runtime_predictor/models/predictor.pkl{,.bak}`
（PVC `runtime-predictor-models`，1 Gi RWO，預設 storage class）。

## M6：Predictor → Lua → backfill 端到端

### 目標
把 M5 service 接到 M2 lua plugin。Lua sbatch 時呼叫 predictor，把回傳 seconds 寫成 `job_desc.time_limit`（user 沒填或填得超大時才覆蓋）。

### 產出檔案
- `chart/scripts/job_submit.lua` — 加 HTTP call（`socket.http` + `lua-cjson`）
- `chart/templates/configmap-job-submit.yaml` — 注入 `predictor_url`
- `chart/values.yaml` — `slurm.jobSubmit.predictor.{enabled, url, timeoutMs, fallbackHours}`
- `chart/tests/job_submit_test.yaml` — 加 2 條

### 驗收條件
- [x] sbatch 一個 job，controller log 確認 lua 拿到預測值 + 寫進 time_limit
- [x] M8 simulator `bf_rate` 顯示 backfill scheduling rate 上升（E2 0.912 → E4 0.963）；live `bf_*` Slurm metric 留給 E7 驗證
- [x] predictor 掛掉時 lua fallback 不影響 sbatch（`pcall` 包 HTTP call）

### 風險
- slurmctld lua plugin 是 in-process call，HTTP timeout 卡住整個排程器 — 必須設 `timeoutMs=200` 上限

### 實機驗收（2026-05-07，custom-sched）

`bash scripts/verify-predictor-lua.sh` 五道閘門全綠（host: lua5.3 +
curl 8.5；predictor 跑在 .venv-m5 / lgbm 4.6 / fastapi 0.136）：

| Gate                                                   | 結果 |
|--------------------------------------------------------|------|
| 1. M5 model 訓練（synthetic-with-signal, n_train=480） | mae_log=0.423 |
| 2. uvicorn 起 + /readyz                                | model=lgbm-v1, n_train=480 |
| 3. 渲染 lua + luac syntax check                        | 305 行，no error |
| 4. lua → 真 curl → 真 predictor                        | rc=0、time_limit=10 min（pred ≈620 s）+ `[predictor] applied` log |
| 5. predictor 掛掉，再跑 lua                            | rc=0、time_limit 保持 0、`[predictor] skipped (predictor-unavailable)` |

額外的 lua-unit（`tests/lua/score_test.lua`）：**34/34**（M3 27 + M6 7：
parse_predict_response 三條、build_predict_body 兩條、predictor_gpu_type、
maybe_apply_predicted_time_limit gating）。

helm-unittest：`tests/job_submit_test.yaml` **16/16**（+4 M6：disabled
default、URL/timeout/fallback inline、applyTimeLimit、defaultGpuType），
chart 總計 **93/93**。

實作關鍵：
- 走 `os.execute` 風格的 `io.popen("curl -fsS --max-time …")` — slurmctld
  跑 lua5.1，jammy `main` 沒 lua-socket / lua-cjson；shell-out 給 curl 是
  最少依賴。`docker/controller/Dockerfile` 加 `curl` 一行。
- Body 用 `string.format` 手工 JSON（response 也用單一 regex 抓 `pred_seconds`）
  ─ 不引 cjson。
- 整段呼叫包在 `pcall(call_predictor, …)`，加 curl 自身 `--max-time` 雙保險。
- 寫回 `job_desc.time_limit` 只在 user 沒填 (`time_limit=0`) 或 user 填的
  walltime > 預測 ×2 時觸發；其他情況留 user 設定（避免覆蓋顯式 SLA）。
- `bf_*` rate 對 M1 baseline 的提升留給 M8（trace replay 取 200~500 jobs
  比較 fcfs / multifactor / score / score+predictor 四線）— M6 只負責
  把 plumbing 接通。

實際渲染（disabled default）：
```
PRED_ENABLED          = false
PRED_URL              = "http://runtime-predictor:8080/predict"
PRED_TIMEOUT_S        = 200 / 1000.0
PRED_FALLBACK_HOURS   = 4
PRED_APPLY_TIME_LIMIT = true
PRED_DEFAULT_GPU_TYPE = "rtx4070"
```

## M7：Fragmentation detector + 自動 requeue

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
- [x] 模擬 fragmentation：4 個 mps:25 占滿 → 1 個 mps:50（pending）→ operator 偵測 + requeue 2 個 mps:25 → log 看得到 `requeue_decision`
- [ ] requeued job 自己 resume，loss 從 ckpt 接續（DDP MNIST or 小 LLaMA toy）— M8 simulator 未扣 checkpoint reload cost，留給 E7/live-cluster 驗證
- [x] rate-limit 生效：強制觸發 6 次/小時，第 6 次 reject + warn
- [x] `verify.sh` 全綠（chart-side：97/97 helm-unittest）

### 風險
- 「以為能解卡，requeue 完 GPU 被別 job 搶走」— mitigation：requeue 後 5 秒內若 pending 還沒 start，回滾 + log
- ckpt resume 依賴 user code — 提供 reference template，evaluation 用 reference workload 跑

### 實機驗收（2026-05-07，custom-sched）

`bash scripts/verify-fragmentation.sh` 五道閘門全綠：

| Gate                                                       | 結果 |
|------------------------------------------------------------|------|
| 1. pytest `operator/tests/test_fragmentation.py`           | 16/16 |
| 2. helm-unittest（operator + workers，M7 cases）           | 19/19 完整套件；總綱 97/97 |
| 3. 4×mps:25 occupancy + mps:50 pending → requeue 2 victims | `decision.targets=('r1','r2')`，理由 log 完整 |
| 4. rate-limit：強制 6 次決策                                | 5 通過、第 6 次 `rate-limited:hourly-cap (5)` 被拒 |
| 5. shadow mode                                             | decision 仍產生、actuator 0 次被呼叫、`requeued=()`  |

實作關鍵：
- `operator/fragmentation.py`：純 stdlib（無 prometheus / kubernetes / urllib），把
  detector / decider / reconciler 三層解耦，每層都可獨立 fixture 測試。
- `FragmentationDetector` 產生「所有」eligible 受害者（不在偵測階段預先剪枝），
  讓 `RequeueDecider` 用 `priority + runtime` 二級排序去挑最少價值損失的子集。
- 雙層 rate limit：`min_interval_seconds`（同一秒不會兩次）+
  `max_requeues_per_hour`（sliding window，不需持久化；operator 重啟後等一小時
  自動恢復額度）。
- Slurm REST adapter（`jobs_from_slurm_rest`、`nodes_from_slurm_rest`）解析
  欄位的優先序對齊 live cluster 真實 schema（見 docs/note.md 問題 14）：
  - **總量**：節點配置 `gres="gpu:rtx4070:1,mps:rtx4070:100"` 取出 100 為
    `total_mps`；節點若沒有 mps gres token（CPU-only pool），總量直接給 0，
    避免 detector 把 CPU node 當作 MPS job 可塞之處。
  - **已用 slot**：優先讀 `tres_used="cpu=4,gres/mps=50"` 的 `gres/mps=N`
    （這才是 slot 數）；fallback 才看 `gres_used`，因為 live `gres_used` 是
    `mps:rtx4070:1(IDX:0)` 形態，那個 `1` 是裝置數不是 slot 數。
  - **節點可用性**：`state` 含 drain / down / drain* / not_responding 等
    任一字面都直接 `free_mps=0`，不讓 detector 把 drained node 當作可放
    置目標（`_node_is_available` + `_UNAVAILABLE_NODE_STATES` 處理 list /
    `"DOWN+NOT_RESPONDING"` / 單字串三種 schema）。
  - **job 端**：`tres_per_node="gres:mps:25"` 推 `mps_req`，跟 lua plugin 的
    `parse_mps_req` 行為對齊。
- `app.py` 整合：獨立 daemon thread (`fragmentation-reconciler`)，跟主 reconcile
  queue 分離 — 一個慢 `scontrol requeue` 不會卡 scale-up/down 的決策。預設
  `FRAGMENTATION_SHADOW_MODE=true`，跑一個 release cycle 觀察 log/metric 後
  才能翻成 false。
- Metrics：`slurm_operator_fragmentation_score`（per-node free MPS 的
  coefficient of variation）、`slurm_operator_fragmentation_blocked_jobs` (gauge)、
  `slurm_operator_requeue_total{reason}` (counter，bucket: unblock /
  rate-limited / no-fragmentation / no-victims)、`slurm_operator_requeue_victims_total`。
- `chart/templates/configmap-task-prolog.yaml` 加 `20-resume-helper.sh`：
  drop 一份 `resume_from_checkpoint()` shell 函式到 `$SLURM_TMPDIR/slurm-resume-<JID>.sh`，
  export `SLURM_RESUME_HELPER` 給 user sbatch `source` — 真實 ckpt resume 由 user
  code 用 `$RESUME_FROM_CHECKPOINT` 接（reference template 留給 M8 workload）。

> **M7 邊界**：shadow-mode plumbing 已在 k3s + RTX 4070 live cluster 驗證通過
> （2026-05-07）。情境 small1 mps:25 RUNNING + score-demo-bigfit mps:80
> PENDING priority 9999 + gpu-rtx4070-1 drained，operator 每 15s 吐：
>
> ```
> event_type=requeue_decision  score=1.0  blocked=["86"]
> reason="unblock 86 (priority 9999, mps_req 80) on slurm-worker-gpu-rtx4070-0:
>         requeue 1 job(s) freeing ~25 slots"
> shadow=true  target_jobs=["85"]  unblocks=["86"]  requeued_jobs=[]
> ```
>
> 過程踩到的 adapter 三處對 schema 認知錯誤（`gres_used` ≠ slot 數、CPU-only
> 節點 total_mps 該為 0、drained 節點該排除）已修並補 unit test，詳細
> root cause + 修法見 docs/note.md 問題 14。實機 `scontrol requeue`（翻
> shadowMode=false 把真正 actuator 接上）未在 M8 simulator evaluation 中宣稱完成，
> 仍排在 E7/live-cluster 驗證。

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
- [x] 7 組實驗 raw data 齊全 — E1..E6 simulator runs + E7 harness
- [x] 7 張圖出爐（CDF of JCT、box of slowdown、line of utilization over time）
- [x] eval-writeup.md 約 8–12 頁、每張圖 1 段論述

### 實驗結果（2026-05-07，2026-05-11 二度修訂）

M8 產物：`eval/results/`、`eval/figures/fig{1..8}.{png,pdf}` 與
`docs/eval-writeup.md`。

**修訂歷史**：
- 第一輪（同日早）：修掉 sim `try_fragmentation_reconcile` 把 victim
  `metrics.submit_ts` reset 的 bug、改用 5 seeds、加 `--ckpt-reload-cost`。
- 第二輪：把 trace 廣度從單一 Philly-like 擴成 3 個 family（philly /
  burst / ali），驗證 negative result 是否 generalises。新增 fig8
  cross-trace。

**跨 trace 主表**（5 seeds 每 cell，數字為 paired same-seed Δ 對比前一階段）：

| | philly | burst | ali |
|---|---:|---:|---:|
| E2 vs E1（vendor 對 FCFS） | −72% | −63% | −0.5% |
| E3 vs E2（純 score） | +4.9% | +4.1% | 0% |
| **E4 vs E2（M5 predictor）** | **−20.1% ↓** | **−28.7% ↓** | −0.08% |
| **E5 vs E4（M7 fragmentation）** | **+33.1% ↑** | **+60.9% ↑** | **+6.0% ↑** |
| E5b vs E5（去掉 ckpt cost） | −5.8% | −0.7% | −1.9% |
| E6 weight sensitivity spread | 10.6% | 28.5% | 0.1% |

↓/↑ 表示 95% CI 不跨 0。

**主要結論**：
- M5 runtime predictor 在有 contention 的 trace 上 statistically significant
  改善 mean JCT；ali 沒 contention 所以幾乎沒差。Predictor 的價值正比於排隊壓力。
- M7 fragmentation 在三個 distribution 完全不同的 trace 上全是 net negative，
  CI 都不跨 0。Negative result generalises，不是 philly artefact。
- E5b 把 ckpt reload cost 設 0 也救不回來，問題出在 victim 重跑時失去的
  in-flight progress，不是 reload overhead。
- E6 sensitivity 跟 trace contention 等級正相關（ali 0.1% / philly 10.6% /
  burst 28.5%），M9 RL tuner 在高 contention trace 上才會有意義。

M7 救援方向（給 future work）：(1) victim selection 加「已執行時間 / 完成比例」
penalty；(2) 改 preempt+suspend 保留 GPU memory；(3) 只在 predictor 估計 head
剩餘時間 < victim 剩餘時間時觸發。詳見 `docs/eval-writeup.md` §4 C4。

### 後續改進進度（2026-05-11 完成）

原始 handoff #1..#6 都有對應的動作；M5 predictor live 部署留作下一階段。

- #1 E7 live cluster：跑了 4 個 pass paired comparison（vendor / our M3 / our_pred M3+M5 / our_pred_hetero_v2 with retrained predictor）。Headline 是 our_pred 比 vendor 快 −57.75%、20/20 改善。但 our_pred vs our 只差 0.13% — predictor 在 homogeneous workload 上沒帶來邊際改善。v2 pass 用 distribution-matched 重訓模型再驗證，跟 our_pred 仍然只差 −0.08%。結論精確化：predictor 機制都對，但 e7 workload runtime 跨度只 6×、score function 的 ε·f_runtime_short 換算到 priority 只值 43 秒等待換算——signal 太弱被 noise 淹過。**Predictor 要發揮，workload 必須跨多個量級**（sim Philly trace 跨 100×、live e7 跨 6×）。詳見 `docs/eval-writeup.md` §7.6–§7.8。
- #2 ckpt resume cost：`sim/runner.py` 加 `--ckpt-reload-cost`、E5b 對照組；E5b vs E5 顯示 cost 只佔 6%，問題是 lost progress 不是 reload。
- #3 受控 rollout：`chart/values.yaml::operator.fragmentation` 預設 enabled=false、shadowMode=true，加上 M8 negative result 警告註解，不開直到 victim selection 改進。
- #4 trace coverage：新增 `generate_burst_heavy` / `generate_ali_like`，跨三個 trace × 5 seeds 驗證 negative result generalises。
- #5 sensitivity grid：升級到 5×5（α × δ），三個 trace 都跑。
- #6 production claim 分層：eval-writeup §4 把 claim 分 simulator result / shadow observation / live result 三層，未經 live 驗證的不在 chart default 開。

### 風險
- 結論不顯著（score ≈ multifactor）— M3 / M6 之後就先跑 mini-eval 抓問題，不要拖到 M8 才發現

## M9：Bandit / PPO weight tuning

### 目標

把 score function 的 (α, δ, ε) 三個係數當 bandit arm，reward = −mean_JCT（hours）。
UCB1 在 sim 快速找到接近最佳的 weight 組合；live 上以同一 service 做 outer-loop，
每 N 個 completed jobs 更新一次，讓叢集自適應工作負載分佈。
β (vramFit) 固定不調（M8 eval 顯示其敏感度低）。

### 實作產出

```
services/weight_tuner/
  bandit.py          UCB1Policy + LinUCBPolicy（共用 BanditPolicy 介面）
  sim_env.py         SimPull — 包 sim.runner 的 pull(arm, context) callable
  serve.py           FastAPI :8003（/weights /feedback /stats /healthz）
                     + background asyncio collector（每 300s 拉 slurmrestd）
  requirements.txt   fastapi uvicorn pydantic numpy（無 torch，image 輕量）
  Dockerfile
  tests/test_bandit.py

chart/templates/weight-tuner/deployment.yaml   Deployment + ClusterIP :8003 + NetworkPolicy
chart/values.yaml    weightTuner block（enabled=false 預設）
chart/templates/configmap-job-submit.yaml
  — WT_ENABLED / WT_URL block：plugin load 時 curl /weights，pcall 保護
  — f_pred_runtime(job_desc)：改為呼叫 call_predictor()，1−pred_s/fallback 正規化
chart/templates/network-policy.yaml   controller egress → weight-tuner:8003
```

### Sim 評估（2026-05-11）

27 arms（α ∈ {0.10, 0.40, 0.70} × δ ∈ {0.05, 0.20, 0.40} × ε ∈ {0.00, 0.30, 0.60}），
philly 合成 trace，120 rounds × 3 families。

| Policy | eval mean JCT (h) | sim runs |
|---|---:|---:|
| random | 3.217 | 120 |
| **UCB1** | **2.587** | 120 |
| LinUCB | 2.745 | 120 |
| M8 grid-best (static) | 2.511 | 375 |
| Per-context oracle | 2.448 | full |

**關鍵發現**：oracle 與 static-best 只差 2.5%，contextual tuning 沒有 headroom；
LinUCB 被 ridge regression fit noise 拖慢。**UCB1 用 1/3 的 sim 預算達到接近 grid-best 的結果**。
PPO 未做（oracle ceiling 只多 2.5%，預期收益 < 5%，sim-only 已足以回答研究問題）。

完整 writeup 在 `docs/eval-writeup.md` §8。

### Live 架構（整合 M10-D，2026-05-13）

M10 Phase D 已驗證 `rl-scheduler` live shadow pipeline，UCB1 outer loop 在此基礎上平行疊加：

```
Lua job_submit.lua（plugin load，一次）
    │  curl GET /weights → (α, δ, ε)，覆蓋 A/D/E upvalue
    ▼
compute_score(job_desc)
    ├─ f_mps_fit, f_vram_fit, f_fragmentation（本地計算）
    └─ f_pred_runtime(job_desc) → curl POST /predict → 1 − pred_s/fallback
                                  （PRED_ENABLED=true 時真實呼叫 runtime-predictor）
    │  score = α·f_m + β·f_v − δ·f_f + ε·f_p → delta → job_desc.priority
    ▼
rl-scheduler /decide（per-job，shadow mode）
    └─ MaskablePPO inner loop：100% abstain（policy 尚不可靠）

weight-tuner background collector（每 300s）
    └─ slurmrestd /jobs → filter COMPLETED since last poll
       → −mean_JCT_hours → POST /feedback → UCB1.update(arm, reward)
```

- **arm** 維度：(α, δ, ε)，27 個離散格；β=0.20 固定
- **reward**：−mean_JCT_hours（越短越好，bandit 最大化）
- M10 Phase F（D-LinUCB outer loop）：直接在 serve.py 切 `policy=linucb`，無需架構改動

### Live 實驗結果（2026-05-13，helm rev. 33–34）

**部署（k3s, ns/slurm）**

```
weight-tuner       10.43.140.94:8003   1/1 Running
rl-scheduler                   :8002   1/1 Running
runtime-predictor              :8080   1/1 Running
```

**Round 1 — arm load + f_pred_runtime stub（rev.33，ε=0.00）**

Controller 啟動 log：
```
[weight-tuner] loaded arm α=0.100 δ=0.050 ε=0.000
[score-m3] loaded (apply=true, weights α=0.1 β=0.2 γ=0 δ=0.05 ε=0)
```

13 shadow jobs（`--wrap='sleep 2'`，-p cpu）：
```
jobs_scored=13   mean_score=0.2000   delta=+200   rl_abstain=13
```
score = 0.10·1.00 + 0.20·0.50 + 0.00·0.50 = 0.200（f_pred_runtime 此時仍 stub=0.5 但 ε=0 無影響）

**UCB1 手動 feedback（t=8，8/27 arms tried）**

| arm (α, δ, ε)   | n | mean reward   |
|------------------|---|---------------|
| (0.4, 0.4, 0.3) | 1 | −0.000300 ← best |
| (0.4, 0.05, 0.3)| 1 | −0.000400 |
| (0.7, 0.05, 0.3)| 1 | −0.000500 |
| (0.4, 0.2, 0.0) | 1 | −0.000600 |
| (0.7, 0.2, 0.0) | 1 | −0.000700 |
| (0.1, 0.05, 0.0)| 1 | −0.000800 初始 arm |
| (0.1, 0.2, 0.0) | 1 | −0.000900 |
| (0.1, 0.4, 0.0) | 1 | −0.001000 worst |

**Round 2 — f_pred_runtime 接線完成（rev.34，ε=0.30）**

`f_pred_runtime` 改為呼叫 `call_predictor()`，正規化 `1 − pred_s / PRED_FALLBACK_SECONDS`：

```lua
function f_pred_runtime(job_desc)
  if not PRED_ENABLED then return 0.5 end
  local ok, success, pred_s = pcall(call_predictor, job_desc)
  if not ok or not success or not pred_s or pred_s <= 0 then return 0.5 end
  return clamp01(1.0 - pred_s / PRED_FALLBACK_SECONDS)
end
```

UCB1 下一 arm = `(0.10, 0.05, 0.30)`（UCB 探索未試過的 arm），controller reload：
```
[weight-tuner] loaded arm α=0.100 δ=0.050 ε=0.300
[score-m3] loaded (apply=true, weights α=0.1 β=0.2 γ=0 δ=0.05 ε=0.3)
```

測試 job（`sleep 3`, -p cpu）結果：
```
score=0.4968  delta=497  mps_fit=1.00  vram_fit=0.50  frag=0.00  pred=0.99
```
驗算：0.10·1.00 + 0.20·0.50 + 0.30·0.99 = 0.497 ✓

predictor 回傳 `pred_seconds ≈ 180s`（`sleep 3`），f_p = 1 − 180/14400 ≈ 0.99。
Score 從 0.200（ε=0）跳到 0.497（ε=0.30 × pred=0.99），**predictor 貢獻已生效**。

### 驗收條件

- [x] UCB1 sim 上 120 rounds，eval JCT 2.587h（vs random 3.217h，↓19.6%）
- [x] `GET /weights` p99 < 50ms；Lua pcall 保護，失敗 fallback 到 chart 預設
- [x] f_pred_runtime 接 runtime-predictor：ε·f_p 納入 score 計算
- [x] `weightTuner.enabled=false`（預設）不影響任何現有路徑
- [x] live pipeline 端到端驗通：arm load → score → delta → priority 生效
- [ ] 部署 24h 後 UCB1 auto-feedback 收斂（top-3 arm pulls ≥ 60%）— 需 GPU workloads

### 已知侷限

- **JCT 信號弱**：CPU `sleep` jobs JCT ~2s，arm 差異訊號雜訊比極低。
  有效收斂需 GPU training workloads（JCT 數十分鐘）。
- **RL abstain = 100%**：MaskablePPO policy 在此叢集 workload 不可靠，
  score+WT 是目前唯一有效的優先順序調整路徑。
- **D-LinUCB upgrade**（M10-F）：切換 `policy=linucb` 即可，架構無需改動。

## M10：Deep RL scheduler

M9 的 UCB1 都是 bandit，只決定「權重組合」、不決定「下一個跑哪個 job」。在此階段 (M10) 升級成 Deep RL scheduler。目標是讓 DRL policy 直接學排程決策本身（policy 輸出 = top-K queue 中下一個 schedule 的 job），把 M3 score function 從「固定形式 + 學權重」變成**學習 policy 來取代手寫公式**。

考慮到小叢集樣本量（一天 ~200 jobs）遠低於 online DRL 需求（10⁵–10⁶ episode），因為樣本量級差 3 個數量級，因此 M10 採取 **sim-trained PPO + Sim2Real fine-tune** 三段式設計，並保留 D-LinUCB 作為 outer-loop weight tuner / fallback baseline（階層式結構）。三階段如下：

1. Sim 訓練：Gymnasium wrapper 包 `sim/runner.py`，PPO 跑 10⁵–10⁶ episode 拿到 sim-optimal policy。Sim throughput 不受 wall-clock 限制。
2. Sim2Real 微調：sim 策略部署到 live cluster，用 RLPD（Ball ICML 2023）混合 sim trajectory（offline）+ live trajectory（online）持續做 fine-tune。RLPD 在 ~10³ live samples 級別已能拉近 sim-to-real 差距。
3. 階層式：外層 D-LinUCB 在小時尺度調 (α, β, δ, ε) 4 個 reward shaping 超參數，內層 DRL 在 per-decision 尺度排程。

### MDP 設計

**State**（observation space）：permutation-invariant encoding of pending queue + cluster
- Top-K=16 pending jobs，每個 job 11 維 feature：`[mps_req/N, gpu_count, gpu_type_onehot(4), runtime_pred_log, wait_time_log, age_log, deadline_remaining, retry_count]`
- 每個 worker node：`[free_mps/N, free_vram/N, running_job_count]`
- 全域：`[queue_len_log, predictor_spread, fragmentation_index, time_of_day_sin/cos]`
- Padding mask 處理 queue < K 情況

**Action**：discrete head over top-K + no-op
- `a ∈ {schedule_job_1, ..., schedule_job_K, no_op}`
- no-op 在所有 K job 都 blocked 時 emit，等下個 timestep
- M10 v1 不直接做 preemption decision；preemption 留給未來的 M12（migration / 優先 preemption）
- M10 v2（可選）擴成 multi-head 加 preempt action

**Reward**：dense + sparse 混合
- 每個 timestep：`r_t = -ΔΣ(wait_time_pending) / 1000`（pending wait 累積懲罰）
- Job 完成時：`r_done = -log(JCT / pred_runtime)`（slowdown 懲罰，跟 M8 metric 對齊）
- Discount γ_rl = 0.99

**Episode 邊界**：sim 一條 trace（philly/burst/ali 各 ~500 jobs）= 一個 episode；live 用 4 小時 sliding window 切 episode boundary

### Policy / Value 架構

```
JobEncoder(MLP 11 → 64)  ── for each of K pending jobs
        ↓
Mean-Pool 或 Set-Attention  ── permutation-invariant
        ↓                    (v1 mean-pool，v2 self-attention)
NodeEncoder(MLP 3 → 32)  ── for each node
        ↓
Concat + GlobalFeat (5 dims)
        ↓
Trunk(MLP 256 → 128)
        ↓                    ↓
Policy head             Value head
(K+1 logits, masked)    (V(s) scalar)
```

~80k 參數，比 Decima GNN encoder 小一個量級。Mean-pool 對 K=16 queue 已足夠（小叢集 queue 通常 < 50），Set Transformer 留 v2 ablation。

引用：
- Zaheer, M. et al. "Deep Sets", **NeurIPS 2017** — permutation-invariant encoder 理論基礎。
- Lee, J. et al. "Set Transformer", **ICML 2019** — v2 attention encoder reference。
- Vinyals, O. et al. "Pointer Networks", **NeurIPS 2015** — variable-length 候選集合 action 範本。

### 訓練演算法

**Phase 1 — Sim PPO**（Schulman 2017）
- 4 parallel sim env、batch=2048、minibatch=256、4 epoch per update
- LR=3e-4 cosine decay、entropy coef=0.01
- Target：10⁵ episode ≈ 4–6 GPU 小時（第二台機器跑剛好）

**Phase 2 — Sim2Real Fine-tune（RLPD，Ball ICML 2023）**
- 50% offline minibatch（sim trajectory replay）+ 50% online minibatch（live trajectory）
- SAC-style critic + 同 policy 架構
- LayerNorm + 高 UTD ratio（update-to-data=20）是 RLPD 關鍵
- 預期 ~500 live samples 即可看到 fine-tune 收斂

**Phase 3 — Hierarchical 整合**
- 外層 bandit / D-LinUCB 跑 reward shaping coefficient（例如 `β_jct`, `β_slow`）
- 內層 DSAC 用當前 coefficient 算 reward，並做 masked scheduling action update
- 外層每個 round 根據 eval mean JCT 更新 arm reward（`−mean_JCT_hours`），內層 per-decision fine-tune
- 引用：Pateria, S. et al. "Hierarchical Reinforcement Learning: A Comprehensive Survey", **ACM Computing Surveys 2021**；Nachum, O. et al. "Data-Efficient HRL" (HIRO), **NeurIPS 2018**

**Phase E 初跑結果（2026-05-14）**

Hierarchical DSAC 已跑完 5 個 outer rounds；最後一輪 UCB 選到
`Arm(β_jct=1.0, β_slow=0.5)`，內層訓練 1000 steps 後得到：

```
JCT=1.576h  reward=-1.5759  elapsed=44s
*** new best — dsac.pt updated ***
Hierarchical DSAC best JCT : 1.576h (Arm(β_jct=1.0, β_slow=0.5))
```

`dsac.pt` 已更新為本次 best checkpoint。這是 single-run 訓練結果；正式結論仍需跑
philly / burst / ali × 5 seeds 的 paired evaluation，再跟 score / PPO 對照。

**Phase E 正式 paired evaluation（2026-05-14）**

`eval/scripts/eval_hierarchical.py` 已跑完 philly / burst / ali × 5 seeds（N2×2gpu,
n_jobs=300, n_outer=5, n_inner=1000）。Paired CI（`score − hier`，正 = DSAC 贏）：

| Family | mean score JCT | mean hier JCT | Δ(score−hier) | 95% CI | 結論 |
|---|---:|---:|---:|---:|---|
| philly | 11.742h | **7.638h** | +4.104h (+35.0%) | [−4.651, +12.859]h | 平均贏但不顯著 |
| burst | **10.430h** | 15.226h | −4.796h (−46.0%) | [−13.002, +3.410]h | 平均輸 |
| ali | **0.822h** | 1.525h | −0.703h (−85.5%) | [−1.095, −0.311]h | 顯著輸 |

結論：hierarchical DSAC 還不能取代 score scheduler。philly 有可追的正向訊號，但
burst / ali generalization 不穩，ali 已顯著 regression；live production path 維持
score + weight tuner，RL 保持 shadow / fallback。

### 實作位置

```
sim/gym_env.py                 # 新檔：gym.Env wrapper for runner.py
                                #   reset(), step(action), spaces, info
services/rl_scheduler/
  policy.py                    # DRL policy + value network (PyTorch)
  ppo_train.py                 # sim training script
  rlpd_finetune.py             # Sim2Real fine-tune
  serve.py                     # FastAPI /select_action endpoint
  state_store.py               # checkpoint policy weights to PVC
services/weight_tuner/         # 沿用 M9 + 加 D-LinUCB（外層 reward shaping）
chart/templates/rl-scheduler.yaml          # 新 Deployment (GPU=1 inference)
chart/templates/configmap-job-submit.yaml  # lua 改 call /select_action
```

Slurm 側：原本的 score function 變 fallback path。`job_submit.lua` 在 backfill 階段呼叫 RL serve endpoint 拿 action；endpoint timeout 或 confidence 低時 fallback 回 M3 score scheduler。

### 部署規劃

| 階段 | 內容 | 時程 |
|---|---|---|
| Phase A | Gymnasium wrapper（`sim/gym_env.py`），unit tests 驗證 spec 一致 | 1 週 |
| Phase B | PPO training pipeline + sim 上 vs fcfs / multifactor / score paired CI | 1.5 週 |
| Phase C | RLPD fine-tune pipeline + serve endpoint + lua 整合 | 1 週 |
| Phase D | 分別部署 DRL scheduler 比對 score scheduler | 4 天 |
| Phase E | Hierarchical 加 D-LinUCB outer loop + ablation | 5 天 |

### Safety net

DRL inference 包同樣 guardrail wrapper：

```python
def select_with_guardrail(obs, cluster_state):
    a_rl, v_rl = rl_policy(obs)

    # Rule 1: value head 估計過低 → fallback score
    if v_rl < V_THRESHOLD:
        return score_scheduler_action(cluster_state), "fallback:low_value"

    # Rule 2: policy entropy 過高（不確定） → fallback
    if policy_entropy(obs) > ENTROPY_THRESHOLD:
        return score_scheduler_action(cluster_state), "fallback:high_entropy"

    # Rule 3: queue < 3 不需 RL
    if cluster_state.queue_len < 3:
        return score_scheduler_action(cluster_state), "fallback:queue_too_small"

    # Rule 4: serve endpoint timeout (lua side: HTTP timeout 200ms → fallback)

    return a_rl, "rl"
```

Fallback rate 目標 < 10%（RL 比 bandit 更難一次學好，閾值放寬）。

引用：
- Bouneffouf, D., Rish, I., & Aggarwal, C., "Survey on Applications of Multi-Armed and Contextual Bandits", **IEEE CIM 2020** — learning component 的 safety / fallback pattern 整理。
- Krishnaswamy, V. et al. **NSDI 2023** — systems setting 用 shadow eval 守住 learning component 的具體案例。

### 替代方法分析（為什麼是 PPO + RLPD）

| 方法 | 樣本需求 | 1–2 機可行 | 主風險 | 採用 |
|---|---|:---:|---|:---:|
| **Sim-PPO + RLPD fine-tune** | sim 10⁵ + live ~500 | ✅ 5–6 週 | sim-to-real gap、reward shaping 工 | ✅ 主軸 |
| **D-LinUCB（Russac NeurIPS 2019）** | ~300–500 jobs | ✅ 2 週 | 只調權重、不直接學 policy | ✅ Phase F outer-loop / baseline 對照 |
| Decima 原版 GNN（Mao SIGCOMM 2019） | 10⁵ episode | ✅ sim 內 | DAG-aware 設計過剩（我們無 DAG） | ❌（簡化版 inspired） |
| DreamerV3 model-based | ~10³–10⁴ live | ⚠️ 需學 world model | world model 學歪 policy 跟著歪 | ❌（Phase B 後備） |
| IQL / ReBRAC offline-only | ~10⁴ transitions | ⚠️ 訓得起來 | dataset 蓋天花板 | ❌（baseline 對照） |
| Pure online PPO/SAC live | ~10⁵–10⁶ episodes | ❌ | exploration 期 JCT 爆炸 | ❌ |
| Decision Transformer | ~10⁴ transitions | ⚠️ sequence-modeling 路線 | systems setting 尚無強案例 | ❌（future work） |
| Online D-LinUCB only | ~300–500 jobs | ✅ | 只調權重，policy 仍是手寫公式 | ❌（被主軸 superset） |

關鍵 insight：**sim 提供「廉價但有 bias 的 reward signal」、live 提供「貴但 unbiased 的 reward signal」**，RLPD 用 ratio mixing 整合兩者，是 2023 之後 sim-to-real 的主流範式，跟 Kubeflux「sim 已存在、live 樣本稀少」的 setting 天然契合。

### 引用文獻

**主方法（Sim-trained DRL + Sim2Real）**
- Schulman, J. et al. "Proximal Policy Optimization Algorithms", **arXiv 2017** — PPO 原始論文。
- Ball, P. J., Smith, L., Kostrikov, I., & Levine, S. "Efficient Online Reinforcement Learning with Offline Data" (RLPD), **ICML 2023** — Sim2Real fine-tune 主方法。
- Song, Y. et al. "Hybrid RL: Using Both Offline and Online Data Can Make RL Efficient", **ICLR 2023** — 並列工作，理論分析 hybrid RL sample complexity。
- Niu, H. et al. "When to Trust Your Simulator: Dynamics-Aware Hybrid Offline-and-Online RL", **NeurIPS 2022** — sim-trust 自適應，RLPD 對照組。

**Cluster scheduling DRL 先導工作**
- Mao, H. et al. "Resource Management with Deep Reinforcement Learning" (DeepRM), **HotNets 2016** — cluster scheduling RL 奠基。
- Mao, H. et al. "Learning Scheduling Algorithms for Data Processing Clusters" (Decima), **SIGCOMM 2019** — GNN + PPO，M11 架構直接前作。
- Park, A. et al. "Park: An Open Platform for Learning-Augmented Computer Systems", **NeurIPS 2019** — systems-RL benchmark，含 cluster scheduling environment。

**架構元件**
- Zaheer, M. et al. "Deep Sets", **NeurIPS 2017**。
- Lee, J. et al. "Set Transformer", **ICML 2019**。
- Vinyals, O. et al. "Pointer Networks", **NeurIPS 2015**。

**Hierarchical RL**
- Pateria, S. et al. "Hierarchical Reinforcement Learning: A Comprehensive Survey", **ACM Computing Surveys 2021**。
- Nachum, O. et al. "Data-Efficient Hierarchical Reinforcement Learning" (HIRO), **NeurIPS 2018**。

**Outer-loop bandit（D-LinUCB，Phase F）**
- Russac, Y. et al. "Weighted Linear Bandits for Non-Stationary Environments", **NeurIPS 2019**。
- Li, L. et al. **WWW 2010**、Auer, P. et al. **Machine Learning 47, 2002** — bandit base citations。

**Offline RL 對照組 / future work**
- Kostrikov, I. et al. "Offline Reinforcement Learning with Implicit Q-Learning" (IQL), **ICLR 2022**。
- Tarasov, D. et al. "ReBRAC: Revisiting the Minimalist Approach to Offline RL", **NeurIPS 2023**。
- Chen, L. et al. "Decision Transformer: Reinforcement Learning via Sequence Modeling", **NeurIPS 2021**。
- Hafner, D. et al. "Mastering Diverse Domains through World Models" (DreamerV3), **arXiv 2023 / Nature 2025**。

**系統 baseline（non-ML 對照）**
- Zheng, P. et al. "Shockwave: Fair and Efficient Cluster Scheduling for Dynamic Adaptation", **NSDI 2023**。
- Jayaram Subramanya, S. et al. "Sia: Heterogeneity-aware, Goodput-Optimized ML-cluster Scheduling", **SOSP 2023**。
- Mohan, J. et al. "Looking Beyond GPUs for DNN Scheduling" (Synergy), **OSDI 2022**。

### 期望結論

跑完 M10 後 thesis 章節變動：

- §3 公式章節從「手寫 5 因子」延伸成「公式作為 fallback / interpretability layer，主決策由 DRL policy 取代」。
- §4 新增 "Deep RL scheduler architecture"：MDP 設計、permutation-invariant encoder、PPO + RLPD pipeline。
- §5 主結果章節三條對照：DRL policy vs score-default vs D-LinUCB-only，paired CI，預期 DRL 在 burst trace（high contention）贏幅最大。
- §6 Discussion 多三段：(a) sim-to-real gap 量化、(b) hierarchical decomposition 為何在小叢集 work、(c) Decima 對照（我們無 DAG、用 Set encoder 取代 GNN）。
- Future work 升級成 Decision Transformer / DreamerV3 / multi-cluster federated RL。

---

## 與 Phase 7（R16 OTel）的關係

Phase 7 OTel 端到端 trace 會把每個 job 的：
1. submit (login pod) → submitted (slurmctld)
2. submitted → priority computed (lua + score function 回傳)
3. priority computed → scheduled to node (backfill 決定)
4. scheduled → first epoch (worker)
5. first epoch → checkpoint
6. checkpoint → completion / requeue (M7 觸發)

每個 step span 內會帶上 score 各 factor 的值、predictor MAE、fragmentation snapshot。**Phase 6 寫 trace span 的成本接近 0**（lua emit log line + operator emit metric 已經有了），等 Phase 7 起手時把這些 log/metric 包成 OTel span 即可。
