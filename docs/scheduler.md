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

## 4. DRL Scheduler ✅

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

### 算法改進（v2，2026-05-14）

三項針對稀疏 JCT reward 和 sample efficiency 的改進，已實作整合進 `sim_train.py`：

#### 1. n-step Returns（n=10）

**問題**：JCT reward 在 job 完成時才給，對應的 scheduling decision 可能在 500+ 步前。1-step TD 的 credit assignment 路徑極長，Q-function 收斂慢。

**方法**：滑動視窗累積 n 步 discounted reward，commit 給 replay buffer：

```
stored_reward = r_t + γ·r_{t+1} + ... + γ^{n-1}·r_{t+n-1}
stored_gamma  = γ^n   (或 γ^k 若在 k < n 處觸到 done)
TD target: y = stored_reward + stored_gamma · (1 − done) · V_soft(s_{t+n})
```

實作於 `_flush_nstep()` (sim_train.py)，ReplayBuffer 新增 `gammas` 欄位，DSAC `update()` 從 batch 讀取 `gammas` 取代固定 `self.gamma`。

#### 2. Score-guided Warmup

**問題**：uniform random warmup 填入品質低的 transition，Q-network 初始訓練信號噪聲大。

**方法**：warmup 期間改用 score scheduler 選 action（`score_warmup=True` 為預設）。Score scheduler 帶有正確的 state→action 相關性，為 Q-network 提供更好的初始訓練樣本。

#### 3. 短 Episode（n_jobs=50，預設）

**問題**：n_jobs=100 → ~2600 steps/episode → 50k steps 只有 19 episodes，Q-function 泛化差。

**方法**：預設 n_jobs=50 → ~800 steps/episode → 50k steps 約 62 episodes（3.3× 提升）。

```bash
# 新預設（三項改進全開，CUDA 版）
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --n-nodes 1 --gpus-per-node 1 \
    --total-steps 200000 --n-jobs 50 \
    --trace-families philly burst ali \
    --seeds 42 43 44 45 46 --device cuda
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

## 5. Attention 架構

### 5.1 問題：MLP 對 Job Queue 的結構盲點

目前 DSAC 把 16 個 job slot 的特徵 flat concatenate 成 176 維向量餵給 MLP。這個設計有兩個根本缺陷：

1. **Permutation sensitivity**：queue 中 job 的順序通常是 submit 時間排序，但最優決策應該與順序無關（「第 3 個 job 最好」vs「最需要 SJF 的 job 最好」）。MLP 必須從頭學 permutation invariance，參數利用率低。
2. **固定 queue 長度**：concat 要求輸入維度固定（16 slots），不同 episode 的 pending job 數不同只能用 padding 解決，增加噪聲。

### 5.2 Attention 為什麼適合 Job Scheduling

Job queue 天生是一個 **set**（無序集合）。Attention 機制對 set 的關鍵優勢：

- **Permutation invariant**：self-attention 對 input 的排列不敏感（Q, K, V 計算與順序無關）
- **Pairwise interaction**：每個 job 能看到其他所有 job 的特徵（「這個 job 是否會和其他 pending job 搶同一張 GPU？」）
- **Variable-length input**：mask 掉 padding token 即可，不需要固定 queue 長度

### 5.3 相關論文

| 論文 | 核心貢獻 | 與本系統相關性 |
|------|---------|-------------|
| **Decima** (Mao et al., SIGCOMM 2019) | GNN + REINFORCE 對 DAG job scheduling；node embedding → scheduling policy | 最直接的 RL + 結構化 graph model 先例。我們的 job queue 是退化的 DAG（無 dependency）|
| **Attention, Learn to Solve Routing Problems** (Kool et al., ICLR 2019) | Transformer encoder 對 TSP/VRP；pointer attention 直接輸出「選哪個 node」| 架構幾乎可以直接移植：job = node，「選哪個 job 排程」= pointer |
| **Pointer Networks** (Vinyals et al., NIPS 2015) | 用 attention 作為 pointer 指向 input set 的某個元素；解 TSP | 理論基礎；明確提出「combinatorial optimization over set = pointer over attention scores」|
| **Set Transformer** (Lee et al., ICML 2019) | 用 Induced Set Attention Block（ISAB）實現 O(mn) 複雜度的 set-to-set 變換 | 直接提供 permutation-invariant set encoding，可作為 Q-network 的 encoder |
| **L2D** (Zhang et al., NeurIPS 2020) | GNN + PPO 對 Job Shop Scheduling（JSP）；disjunctive graph 表示機器衝突 | JSP = 固定 machine，我們是 dynamic GPU。架構啟發但 graph 建法需調整 |
| **Schedformer** 方向 | Transformer 作為排程策略（HPC domain）| 無單一主論文，但有 workshop paper 展示 attention 對 HPC 的適用 |

### 5.4 建議架構：Queue Attention Q-Network

把 DSAC 的 `_QNet` 改為 attention-based encoder：

```
Input:
  jobs     (B, 16, 11)   ← 16 job slots × 11 features
  gpu      (B, 6)         ← GPU state
  topo     (B, 4)         ← topology
  global   (B, 6)         ← global queue stats

Step 1: Per-job embedding
  job_emb = Linear(11 → 64) + ReLU      → (B, 16, 64)

Step 2: Self-attention over job set
  x = TransformerEncoder(d_model=64, nhead=4, nlayers=2)  → (B, 16, 64)
  queue_ctx = x.mean(dim=1)              → (B, 64)   # permutation-invariant

Step 3: Fuse with cluster state
  cluster_ctx = Linear(6+4+6=16 → 64) + ReLU  → (B, 64)
  fused = Concat([queue_ctx, cluster_ctx])      → (B, 128)

Step 4: Q-values
  Q = Linear(128 → 17)                  → (B, 17)
```

**參數量比較**（雙 Q-network）：
- 原 MLP(192→256→256→17)：~130k params
- Attention Q-Network：~50k params（更小，更快，更泛化）

### 5.5 實作考量

- **Padding mask**：當 pending jobs < 16 時，padding slot 應在 self-attention 中被 mask（`src_key_padding_mask`）
- **Positional encoding**：不需要（set 沒有位置意義；加了反而破壞 permutation invariance）
- **與 action mask 的關係**：self-attention 發生在 encoder 層，output 仍是 17 個 Q-value；action masking 依然在 Q-value 層套用，不影響 attention 計算
- **Training stability**：TransformerEncoderLayer 內建 LayerNorm；搭配現有 DSAC 的 orthogonal init 和 twin Q-network 應可穩定訓練

### 5.6 預期效益

理論上，attention 架構主要解決 **sample efficiency** 問題：

- Permutation invariance → 同樣的 job 組合不論排列順序都能泛化，有效樣本數倍增
- 不需要 MLP 浪費容量學 symmetry，更多容量用於學真正的 scheduling heuristic
- Variable-length queue → 在 episode 早期（queue 長）和後期（queue 短）使用同一個 encoder

預計對 50k steps 這個樣本量有最直接的幫助，因為當前 MLP 的 sample complexity 限制正好是瓶頸。

---

## 附錄：引用文獻

**主方法**
- Ball et al., "Efficient Online Reinforcement Learning with Offline Data" (RLPD), ICML 2023
- Schulman et al., "Proximal Policy Optimization Algorithms", arXiv 2017
- Russac et al., "Weighted Linear Bandits for Non-Stationary Environments" (D-LinUCB), NeurIPS 2019

**DRL 排程先導**
- Mao et al., "Resource Management with Deep Reinforcement Learning" (DeepRM), HotNets 2016
- Mao et al., "Learning Scheduling Algorithms for Data Processing Clusters" (Decima), SIGCOMM 2019

**Attention for Combinatorial Optimization**
- Vinyals et al., "Pointer Networks", NIPS 2015
- Kool et al., "Attention, Learn to Solve Routing Problems!", ICLR 2019
- Lee et al., "Set Transformer: A Framework for Attention-based Permutation-Invariant Neural Networks", ICML 2019
- Zhang et al., "Learning to Dispatch for Job Shop Scheduling via Deep Reinforcement Learning", NeurIPS 2020

**Model-based RL 備選**
- Hafner et al., "Mastering Diverse Domains through World Models" (DreamerV3), Nature 2025

**架構元件**
- Zaheer et al., "Deep Sets", NeurIPS 2017
- Lee et al., "Set Transformer", ICML 2019

**系統對照**
- Xiao et al., "Gandiva: Introspective Cluster Scheduling for Deep Learning", OSDI 2018
- Zheng et al., "Shockwave: Fair and Efficient Cluster Scheduling", NSDI 2023
- Jayaram Subramanya et al., "Sia: Heterogeneity-aware Goodput-Optimized ML-cluster Scheduling", SOSP 2023
