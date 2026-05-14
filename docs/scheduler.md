# Scheduler Design

## 1. Slurm 內建調度設定與邊界

Slurm 的調度行為由三組正交設定控制，寫在 `slurm.conf`：

| 設定 | 作用 |
|------|------|
| `SchedulerType` | 主調度演算法，`sched/backfill`（預設）在主排程後找可插隊的小 job |
| `SelectType` | 資源 fit 演算法；GPU/MPS 場景必須用 `select/cons_tres` |
| `SelectTypeParameters` | fit 細部策略：`CR_Pack_Nodes`（bin-pack，塞滿一台再開下一台）vs `CR_LLN`（spread，平均分散，預設） |
| `PriorityType` | job 排序；`priority/multifactor` 支援 age / fairshare / jobSize / QoS 加權 |
| `PreemptType` / `PreemptMode` | 是否允許強制踢走 running job；`REQUEUE` 是 GPU 場景唯一有效模式（`SUSPEND` 留 CUDA context，無實用） |

**Backfill** 的核心假設是「已知每個 job 還剩多久」——它用 `--time`（user 填的 wall time）做規劃。使用者傾向高估 wall time，導致 backfill 空間被壓縮。準確的 runtime 預測可顯著提升 backfill 效益（NERSC Cori 實驗：+5–15% 利用率）。

### 調度邊界

| 能力 | Slurm 內建 |
|------|-----------|
| 新 job 進來時挑「填得最滿」的 node/GPU | ✅ `CR_Pack_Nodes` |
| 大 job 等待時讓小 job 插隊（不延後大 job） | ✅ backfill 預設開 |
| 高優先 job 強制踢低優先 job（kill + requeue） | ✅ `PreemptMode=REQUEUE` |
| 依 QoS / age / runtime 多因子排序 | ✅ `priority/multifactor` |
| GPU memory state 保留的 suspend/resume | ❌ CUDA context 無法凍存 |
| Runtime 把跑到一半的 job 從 GPU1 搬到 GPU0 | ❌ 無任何內建機制 |
| 主動解 fragmentation（evict low-priority running job） | ❌ 不會主動觸發，需外部驅動 |

最後兩列是核心問題：任何主流叢集排程器（K8s / Kueue / Volcano）都無法做到 GPU live migration；能做的只有 **preempt + requeue**，由 application 自己負責 checkpoint resume。

---

## 2. 企業解決方案概覽

| 系統 | 架構 | 對 GPU fragmentation 的處理 |
|------|------|---------------------------|
| **AWS ParallelCluster** | Slurm 包一層，對接 EC2 autoscaling | 靠 scale-out 稀釋；單機多卡場景無解 |
| **AWS Batch / GCP Batch** | VM-level bin-pack（`bin-pack` / `spread` placement group）| 整卡或 MIG slice，不做卡內碎片重整 |
| **GKE + kube-scheduler** | `NodeResourcesFit(MostAllocated)` = bin-pack | 與 Slurm `CR_Pack_Nodes` 概念相同 |
| **Volcano** | K8s 批次，gang scheduling（DDP 必需）、fairshare、preempt plugin | preempt + requeue，無 live migration |
| **Kueue** | Hierarchical quota + cohort，workload preemption | 同上 |
| **Microsoft Singularity** | GPU 時分多工 + checkpoint 感知 preemption | 最接近 live migration，但仍依賴 app-level ckpt |
| **Gandiva (MSR)** | time-slicing + intra-job migration（同節點 GPU 間） | 唯一做到 GPU 間搬移的系統，需 GPU memory snapshot 支援 |

**共通結論**：生產系統面對 GPU fragmentation 一律走兩條路——靠規模稀釋、或 preempt+requeue。沒有人在生產線上做跨節點 GPU live migration。Gandiva 做到同節點 GPU 間搬移，代價是需要特製的 memory snapshot 基礎設施。

---

## 3. 目前客製排程機制

### 整體架構

```
Layer 1 — Score Function + Slurm（全時運作）
Layer 2 — UCB1 Weight Tuner（自適應係數，live）
Layer 3 — DSAC Deep RL（sim 訓練，shadow mode）
```

三層疊加，下層是上層的 fallback。Layer 1 在任何情況下都不停止。

### Layer 1：Score Function

每個 job 在 `sbatch` 時由 `job_submit.lua` 算 score，寫入 `job_desc.priority`：

```
priority = score_gain × (
    α · f_mps_fit        +  // MPS slot 利用率配適
    β · f_vram_fit        +  // VRAM 容量配適
  − δ · f_fragmentation   +  // 放置後碎片代價
    ε · f_pred_runtime       // 短工優先（SJF inspired）
)
```

`f_pred_runtime` 呼叫 **M5 runtime predictor**（LightGBM，FastAPI），從 `job_submit.lua` 透過 `io.popen("curl …")` 取回預測秒數，正規化為 `clamp01(1 − pred_s / horizon)`。Predictor 掛掉時 `pcall` 保護，fallback 到 0.5。

**Fragmentation reconciler（Gandiva-lite）**：Operator 每 15 秒掃 slurmrestd，偵測 pending 高優先 job 被 fragmentation 卡住時，`scontrol requeue` 最低優先的 victim job，讓 GPU slot 釋出。受 rate limit 保護（預設 5 次/小時）。

> **M8 評估結論**：M5 runtime predictor 在有排隊壓力的 trace 上顯著改善 JCT（philly −20.1%、burst −28.7%）。Fragmentation reconciler 在三個 trace 上全為 net negative（philly +33%、burst +61%、ali +6%），原因是 victim 重跑損失 in-flight progress 大於解卡收益；目前維持 `shadowMode=true`，不啟動實際 requeue。

目前 live 係數（UCB1 best arm）：α=0.10, β=0.20, δ=0.05, ε=0.30。

### Layer 2：UCB1 Weight Tuner

`services/weight_tuner/` 將 (α, δ, ε) 組成 27 個離散 arm，背景每 300 秒拉 slurmrestd 收集 completed jobs 的 JCT，計算 reward = −mean_JCT，更新 UCB1 policy。`job_submit.lua` 在 plugin load 時 `curl GET /weights` 拿最新係數。

Sim 評估（120 rounds）：UCB1 達到 eval JCT 2.587h（random 3.217h，−19.6%），接近 grid-search best 2.511h。

### Layer 3：Discrete SAC (DSAC) + Hierarchical

`services/rl_scheduler/dsac.py` 實作 Discrete SAC：隱式 policy π(a|s) = softmax(min(Q₁,Q₂)/α)，twin Q-networks + LayerNorm + auto-temperature + action masking（invalid slot → Q = −1e9）。

**MDP（目前版本）**：
- State obs_dim=193：16 jobs × 11 feats + 4 nodes × 3 feats + 5 global feats
- Action：Discrete(17)，16 個 job slot + no-op（job 選擇，不含 placement）
- Reward：`jct_aligned`（−JCT/scale）或 `shaped`（β_jct·(−JCT/scale) + β_slow·(−log(slowdown))）

**Hierarchical**：D-LinUCB outer loop（小時尺度，選 β_jct × β_slow 9 個 arm）+ DSAC inner loop（per-decision）。`services/rl_scheduler/hierarchical.py`。

**Sim2Real（RLPD）**：offline sim buffer + online live buffer 混合，UTD ratio=4，在 `services/rl_scheduler/rlpd_finetune.py`。

**M10 paired evaluation（philly/burst/ali × 5 seeds，n_inner=1000）**：

| Family | score JCT | hier DSAC JCT | Δ | 顯著 |
|--------|----------:|---------------:|---|------|
| philly | 11.7h | 7.6h | +35% | 不顯著（CI 跨 0） |
| burst  | 10.4h | 15.2h | −46% | 不顯著 |
| ali    | 0.8h  | 1.5h  | −86% | **顯著回退** |

結論：DSAC 1000 inner steps 訓練不足，ali 短 JCT 場景顯著回退。Layer 3 維持 shadow / fallback，不接管 live production。

---

## 4. DRL Scheduler（下一階段規劃）

### 目標

把 Layer 1 + Layer 2 + Layer 3 整合成**單一 DRL policy**，讓模型同時學習：

1. **Job 選擇**：哪個 job 應該現在跑
2. **Placement**：應該放到哪個 node、哪張 GPU、哪段 MPS slot

取代現有的「score 決定順序 + Slurm 決定 placement」分工，讓 placement 也進入 reward 迴路。環境約束：2 台主機 × 2 GPU × MPS enabled（RTX 4070，每 GPU 4 slot）。

### 新 MDP 設計

**State Space（obs_dim ≈ 210）**

```
job queue feats    : TOP_K=16 jobs × 11 dims  = 176
GPU slot feats     : 2 nodes × 2 GPUs × 6 dims =  24
topology feats     : 4 dims
global feats       : 6 dims
```

GPU slot feats（6 dims/GPU）：`free_mps_ratio, running_jobs, vram_used_ratio, gpu_type_onehot(3)`

Topology feats：`intra_bw_ratio`（節點內頻寬），`inter_bw_ratio`（節點間），`ddp_job_ratio`（queue 中多 GPU job 比例），`cross_node_active`（目前跨節點 job 數）

**Action Space — Discrete(65)**

```
A = (job_i, node_j, gpu_k) : i ∈ [0,16), j ∈ {0,1}, k ∈ {0,1}
  + no-op
= 16 × 2 × 2 + 1 = 65
```

Action masking：`job_i` 的 `mps_req > gpu[j][k].free_mps` 時 mask = False；multi-GPU job 需要的 GPU 不夠時 mask = False。DSAC 對 masked action 設 Q = −1e9。

**Reward**

```
r_t = r_placement + r_completion

r_placement（每次 action）：
    α · f_mps_fit(job, gpu)          // 選此 GPU 後剩餘 MPS 配適度
  + β · f_vram_fit(job, gpu)         // VRAM 配適
  − δ · f_fragmentation(state)       // 放置後的碎片代價
  （scale 為小值，例如 × 0.01）

r_completion（job 完成時）：
    β_jct · (−JCT / scale)
  + β_slow · (−log(slowdown))
```

Reward 因子直接繼承 score function 的設計語義，將 heuristic 轉為學習目標。

### DSAC + RLPD 架構圖

```
┌─────────────────────────────────────────────────────────────────────┐
│                        DSAC + RLPD Agent                            │
│                                                                     │
│  Observation S_t (210 dims)                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────┐  │
│  │ Job Queue    │  │ GPU Slots    │  │ Topology   │  │ Global   │  │
│  │ 16×11 = 176  │  │ 2×2×6 = 24  │  │ 4 dims     │  │ 6 dims   │  │
│  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘  └────┬─────┘  │
│         └─────────────────┴────────────────┴───────────────┘        │
│                                    │                                 │
│                              Concat → 210                            │
│                                    │                                 │
│                    ┌───────────────▼───────────────┐                 │
│                    │   Shared Trunk  MLP(210→256→128)│                │
│                    └───────────┬───────────────────┘                 │
│                                │                                     │
│              ┌─────────────────┼─────────────────┐                  │
│              ▼                                   ▼                  │
│   ┌──────────────────┐                ┌──────────────────┐          │
│   │  Q-Network 1     │                │  Q-Network 2     │          │
│   │  MLP(128→65)     │                │  MLP(128→65)     │          │
│   │  Q₁(s,·)         │                │  Q₂(s,·)         │          │
│   └────────┬─────────┘                └────────┬─────────┘          │
│            └──────────────┬───────────────────┘                     │
│                           ▼                                          │
│               min(Q₁, Q₂)  →  apply action mask (−1e9)              │
│                           ▼                                          │
│               π(a|s) = softmax( min(Q₁,Q₂) / α )                   │
│                           │                                          │
│              ┌────────────▼────────────┐                            │
│              │  Action a ∈ Discrete(65) │                            │
│              │  (job_i, node_j, gpu_k)  │                            │
│              │   or  no-op             │                            │
│              └─────────────────────────┘                            │
│                                                                     │
│  Temperature α: auto-tuned via  ∂L_α/∂α = 0  (target entropy)      │
└─────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────┐
│                        RLPD Training Loop                            │
│                                                                      │
│   Sim Env (unlimited)          Live Cluster (~200 decisions/day)     │
│   ┌─────────────────┐          ┌──────────────────────────────┐      │
│   │ KubefluxSchedEnv│          │ Slurm queue poller           │      │
│   │ gym_env.py      │          │ srun --nodelist --gres=mps:N │      │
│   └────────┬────────┘          └──────────────┬───────────────┘      │
│            │  rollout                         │  observe r, s'       │
│            ▼                                  ▼                      │
│   ┌─────────────────┐          ┌──────────────────────────────┐      │
│   │  Offline Buffer │          │  Online Buffer               │      │
│   │  D_sim (large)  │          │  D_live (small, growing)     │      │
│   └────────┬────────┘          └──────────────┬───────────────┘      │
│            └──────────── 50% ┃ 50% ───────────┘                     │
│                              ▼                                       │
│              ┌───────────────────────────────┐                       │
│              │   Mini-batch B (256 transitions)│                      │
│              │   UTD ratio = 4–20 updates/step │                      │
│              └───────────────────────────────┘                       │
│                              │                                       │
│          ┌───────────────────▼───────────────────────┐               │
│          │  Critic update:  L_Q = E[(Q-y)²]          │               │
│          │  y = r + γ · V_soft(s')                   │               │
│          │  V_soft(s') = Σ_a π[min(Q₁,Q₂) − α log π]│               │
│          │  Alpha update:  L_α = E[−α(log π + H̄)]   │               │
│          │  Soft target:   θ' ← τθ + (1−τ)θ'        │               │
│          └───────────────────────────────────────────┘               │
│                                                                      │
│  Safety fallback: if V(s) < V_threshold → score scheduler           │
└──────────────────────────────────────────────────────────────────────┘
```

### Live Training 策略

目標：直接在 live cluster 上訓練（不依賴大量模擬預訓練）。

**推薦演算法：DSAC（Discrete SAC）+ RLPD hybrid**

DSAC 已實作，action masking、twin Q 防止過估計、auto-temperature 適配 sparse reward。RLPD hybrid 在 live sample 稀少的情況下混合 sim rollout（offline buffer）維持穩定性。

```
Live step:
  1. agent 觀測 S_t，根據 DSAC policy 選 (job, node, gpu)
  2. 執行 srun / scontrol（Slurm API）
  3. 觀測 r_t，存入 live buffer
  4. 每 N 步：從 sim buffer（50%）+ live buffer（50%）取 batch，UTD=4–20 梯度更新
```

Safety：value head 估值低或 policy entropy 高時，fallback 到 score scheduler；live buffer 累積 < 500 transitions 前固定 fallback。

**演算法選型對比**

| 演算法 | 離散 action | ~1K live samples | 主風險 | 推薦 |
|--------|:-----------:|:----------------:|--------|:----:|
| **DSAC + RLPD** | ✅ 原生支援 | ✅（sim buffer 補足） | reward shaping 工 | ✅ 主軸 |
| **DreamerV3** | ✅（categorical dist） | ✅ ~100K steps 收斂 | world model 建模 scheduling dynamics 難 | ⭐ 研究備選 |
| **IQL（offline）** | ✅ | ⚠️ 受限 dataset 天花板 | 無法超越 offline dataset | warm-start 用 |
| **TD3 / SAC** | ❌ 連續 action only | — | action space 不符 | ❌ |
| **TD-MPC2** | ❌ | ❌ 需 1M+ steps | 針對連續控制設計；MPPI planning 不適合離散 scheduling | ❌ |
| **PPO (Maskable)** | ✅ | ❌ 需大 batch | live 樣本遠不足 | sim-only 才適用 |

TD-MPC2 的主要問題：(1) 所有 benchmark（DMControl / Meta-World / ManiSkill2）均為連續控制，其 latent world model 假設 smooth dynamics，不適合離散 job arrival 的 stochastic transition；(2) inference 時 MPPI 需要對 candidate action 序列做數百次 world model rollout，scheduling 決策延遲不可接受；(3) 收斂需 1M+ environment steps，live cluster 每天僅 ~200 decisions，差距達 10–40 倍。

**DreamerV3** 是唯一值得列為備選的 model-based 方法：支援離散 action（categorical distribution）、~100K steps 可收斂、world model 可在 sim 中 pre-train 再 fine-tune。但實作量大，建議 DSAC live 驗證通過後再考慮作為研究對照。

### 實作規劃

| 階段 | 內容 | 狀態 |
|------|------|------|
| **Step 1** | 擴展 `sim/gym_env.py`：placement-aware MDP, Discrete(17)，GPU slot + topology feats，env_dims() helper | ✅ |
| **Step 2** | `sim/cluster.py`：`try_allocate_on(job, node, gpu)`, `can_allocate_on()`, `_plan_on()` | ✅ |
| **Step 3** | 5 integration tests：dims match，buffer fill，update loss finite，mask compliance，200-step stability | ✅ |
| **Step 4** | `sim_train.py`：online DSAC training loop（UTD=4，job filter for cluster size）；`eval_dsac_placement.py`：paired t-test + 95% CI vs score baseline | ✅ |
| **Step 5** | `serve.py`：DSAC FastAPI（/healthz /snapshot /decide /act，backward-compat Lua hook，placement node_j+gpu_k in response）；`live_daemon.py`：squeue poller → obs build → srun → transition log，SHADOW_MODE=true default | ✅ |
| **Step 6** | `rlpd_finetune.py`：DSAC-native（KubefluxSchedEnv，gpu_count filter，clean up PPO legacy）；混合 offline sim + online live JSONL；UTD=4–20 | ✅ |
| **Step 7**（可選）| DreamerV3 作為研究對照：用相同 env，比較 sample efficiency 和穩定性 | — |

### 與現有架構的關係

```
現在：
  job_submit.lua (score) → priority → Slurm backfill → select/cons_tres (placement)

新：
  DRL policy(S) → (job, node, gpu) → srun --nodelist --gres
                                      （繞過 Slurm placement，直接指定）
  score scheduler 降為 safety fallback
```

Slurm `SelectType` 仍保留，但 DRL 接管時改用 `--nodelist` + `--gres` 強制指定 placement，不讓 Slurm 自行選卡。

---

## 附錄：引用文獻

**主方法**
- Ball et al., "Efficient Online Reinforcement Learning with Offline Data" (RLPD), ICML 2023
- Schulman et al., "Proximal Policy Optimization Algorithms", arXiv 2017
- Russac et al., "Weighted Linear Bandits for Non-Stationary Environments" (D-LinUCB), NeurIPS 2019

**DRL 排程先導**
- Mao et al., "Resource Management with Deep Reinforcement Learning" (DeepRM), HotNets 2016
- Mao et al., "Learning Scheduling Algorithms for Data Processing Clusters" (Decima), SIGCOMM 2019

**Model-based RL 備選**
- Hafner et al., "Mastering Diverse Domains through World Models" (DreamerV3), Nature 2025

**架構元件**
- Zaheer et al., "Deep Sets", NeurIPS 2017
- Lee et al., "Set Transformer", ICML 2019

**系統對照**
- Xiao et al., "Gandiva: Introspective Cluster Scheduling for Deep Learning", OSDI 2018
- Zheng et al., "Shockwave: Fair and Efficient Cluster Scheduling", NSDI 2023
- Jayaram Subramanya et al., "Sia: Heterogeneity-aware Goodput-Optimized ML-cluster Scheduling", SOSP 2023
