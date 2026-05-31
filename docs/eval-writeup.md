# Kubeflux — Scheduler Evaluation

> 對應 thesis evaluation 章節。圖表 → [`eval/figures/`](../eval/figures/)；原始資料 → [`eval/results/`](../eval/results/)；完整 milestone 規格 → [`docs/scheduler.md`](scheduler.md)。

---

## 1. 動機與貢獻

用 Slurm 排程 HPC workload 已經是標準做法，但有兩個現實沒被內建 scheduler 直接處理：

1. **GPU 共用透過 NVIDIA MPS**。一張卡可切成 100 個 mps slot 給多個 job 同時跑。Slurm 預設 priority 不知道「mps:25 的小 job 應該比 mps:100 的大 job 先排」，所以容易讓一個大 job 卡住一堆小 job。
2. **Job runtime 不可預知**。同一個 user 提交相似 job 的實際時間可以差兩個數量級（幾分鐘到幾小時）。FCFS 跟 multifactor 都假設 runtime 不可知，SJF 反過來要求先知道誰短。

我們想回答的問題：**透過設計 DRL 排程器，能不能在 GPU/MPS workload 上打贏 Slurm 預設？** 

---

## 2. 系統設計

完整設計請看 [`docs/scheduler.md`](scheduler.md)。這節只摘關鍵。

### 2.1 Score function

```
priority = α · f_mps_fit + β · f_vram_fit − δ · f_fragmentation + ε · f_runtime_short
```

每個因子的直覺：

| 因子 | 意義 | 高分代表 |
|---|---|---|
| `f_mps_fit` | job 申請的 mps slot vs node 剩餘 slot 的相對配適度 | 小 job 放空隙剛剛好 |
| `f_vram_fit` | VRAM 需求 vs node tier 的相符程度 | 12g job 放 12g node（不浪費）|
| `f_fragmentation` | 接受 job 後 cluster 的 free-MPS 分散程度（懲罰項）| 接了它後碎片化會變嚴重 |
| `f_runtime_short` | `horizon / (horizon + pred_runtime)`，短 job → 接近 1 | 預計很快跑完 |

預設權重 (α, β, δ, ε) = (0.40, 0.20, 0.20, 0.30)、β 為次要因子鎖在 0.20。M3 完成時 ε=0、M5 接上後切到 0.30。

### 2.2 Architecture（簡圖）

```
sbatch → [slurm-login] ──► slurmctld ──[lua job_submit]──► [predictor service]
                                │                                       │
                                ▼                                       ▼
                          score → priority                       (user, mps, gpu) → pred_seconds
                                │
                                ▼
       ┌──────── pending queue ──────► slurm scheduler + backfill ──► worker pods
       │
       ▼
   [elastic-operator] ──── poll slurmrestd ──► autoscale StatefulSets
                       └── fragmentation reconciler (shadow / live requeue)
```

詳細元件 mapping 在 [`CLAUDE.md`](../CLAUDE.md)。

---

## 3. 評估方法

### 3.1 Trace families

三個 synthetic generator 都在 `sim/loader.py`：

| Family | 特色 | 用來測什麼 |
|---|---|---|
| `philly` | Poisson arrival、log-normal runtime（median ~30 min, p95 ~6h）、~75% 單卡、30% 單卡 job 是 MPS-fractional | baseline workload，跟 Microsoft Philly trace shape 近似 |
| `burst` | 同 job-size mix，arrival 集中在每 6h 一次的 2h 高峰窗 | 高 contention，測 scheduler 在排隊壓力下的行為 |
| `ali` | Alibaba PAI-like：90% 單卡、median runtime ~13 min、60% 單卡 job MPS-fractional、晝夜節律 | 短 job 為主、低 util，測 scheduler 在沒 contention 時是否會 over-engineer |

每 family 跑 5 seeds（42–46），用 paired same-seed diff 做主要比較。Paired diff 把「不同 seed 帶來的 trace 變動」消掉，CI 比 unpaired 緊一個量級。

### 3.2 Live submit-path chaos smoke（2026-05-31）

目的：驗證 optional ML services 失效時，`sbatch` submit path 仍能成功並維持低 latency。測試指令：

```bash
KUBECONFIG=/home/acane/.kube/config SAMPLES=5 PARTITION=cpu \
  bash scripts/chaos/submit-with-services-down.sh
```

環境：live k3s `slurm` namespace；`rl-scheduler` deployment 存在並由 script 暫時 scale to 0 / restore to 1。`runtime-predictor` 與 `weight-tuner` 在本次 live 環境未部署，script 以 warning skip；這仍驗證了 Lua submit path 在 optional service absent/down 時不阻塞提交。原始 TSV：`/tmp/kelpflux-submit-chaos-20260531-235939/latency.tsv`。

| Phase | n | fail | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 5 | 0 | 94 | 98 | 98 | 98 |
| rl-scheduler-down | 5 | 0 | 99 | 105 | 105 | 105 |
| runtime-predictor-down | 5 | 0 | 91 | 110 | 110 | 110 |
| weight-tuner-down | 5 | 0 | 90 | 91 | 91 | 91 |
| all-optional-services-down | 5 | 0 | 95 | 250 | 250 | 250 |

結論：本次 smoke run 中所有 25 次 submit 皆成功，`rl-scheduler` down 時 p95 仍約 105 ms，符合 safe fallback 設計。`all-optional-services-down` 的單筆 250 ms outlier 仍低於目前 Lua curl timeout budget。正式論文數據建議把 `SAMPLES` 提高到 50 或 100，並在 runtime-predictor / weight-tuner 實際部署後重跑。

---

## 4. DSAC Branch — Placement-aware 1×1 Scheduler（2026-05-14）

### 4.1 架構改動

| 項目 | 舊 | DSAC branch |
|---|---|---|
| 演算法 | MaskablePPO（sb3-contrib） | Discrete SAC（自實作，twin Q + LayerNorm）|
| Cluster | 2×2 GPU（4 nodes, 8 GPU） | **1×1 GPU**（1 node, 1 GPU, 4 MPS slots）|
| obs_dim | 視配置 | **192**（16×11 job + 1×6 GPU + 4 topo + 6 global）|
| n_actions | 視配置 | **17**（16 job slots × 1 GPU + no-op）|
| reward | wait-proxy / JCT-aligned | `jct_aligned`（每完成一 job 給 −JCT/scale）|
| Alpha 調節 | N/A | auto-tune（SAC 標準）→ 最終改 fixed_alpha=0.1 |
| 訓練步數 | 500k（PPO） | 50k online（DSAC + UTD=4）|

### 4.2 Score Baseline 修正

本次 session 在 sim 端發現兩個 score scheduler 的系統性問題並修正：

| Bug | 舊行為 | 修正後 |
|---|---|---|
| `f_mps_fit` | `mps_req / MPS_PER_GPU`（固定分母，偏大 job）| `mps_req / best_gpu.free_mps`（bin-pack，獎勵當前狀態最緊配適）|
| `epsilon`（SJF kicker）| 0.0（完全關掉）| **0.30**（短 job 顯著加分）|
| `f_fragmentation`（single-node）| 以 node-level 殘差計算 | 改為 per-GPU MPS 殘差（更精確）|

這些修正讓 score baseline 從 7.067h → 5.381h（philly，**−24%**），在 sim 上已有顯著且可立即部署的改善。

### 4.3 DSAC 訓練結果（1×1 cluster，50k steps，3 family × 5 seeds）

三次 eval，每次只改 alpha 配置，score baseline 固定（已修正版 5.381h）：

| 配置 | Alpha | 訓練 trace | philly DSAC | burst DSAC | ali DSAC |
|---|---|---|---|---|---|
| Run A | 7.389（卡頂，原 bug）| philly-only | 2.663h | 3.513h | 1.145h |
| Run B | 1.649（新上限 clamp）| mixed (p+b+a) | 7.476h | 4.455h | 3.214h |
| Run C | **0.100（fixed）** | mixed (p+b+a) | 12.148h | 14.214h | 2.780h |

**Score baseline（修正後，全三次相同）**：philly 5.381h / burst 6.775h / ali 0.786h

Paired CI（Run C，正 = DSAC 比 Score 好）：

| Family | Δ(score−dsac) | 95% CI | p | 結論 |
|---|---:|---:|---:|---|
| philly | −125.7% | [−262.6%, +11.1%] | 0.063 | 不顯著 |
| burst | −109.8% | [−160.4%, −59.2%] | 0.004 | **顯著輸** |
| ali | −253.5% | [−457.1%, −50.0%] | 0.026 | **顯著輸** |

Run A 看起來 DSAC 贏，是因為 score baseline 尚未修正（7.067h 異常高）。修正後 DSAC 在所有配置下均輸給 score。

### 4.4 Alpha 訓練不穩定性分析

| Run | 問題 | 根因 |
|---|---|---|
| Run A | alpha 全程 = 7.389（= e²，上限）| target_entropy_ratio=0.98 >> 遮罩環境最大熵 log(5)=1.61 → log_α 無限上升 |
| Run B | alpha 全程 = 1.649（= e⁰·⁵，新上限）| Q-network 學到信心 → entropy < target → Adam 向上推 → 卡在新 clamp |
| Run C | alpha 固定 = 0.100（no update）| `fixed_alpha=True`，policy 完全貪婪 → 但 Q-values 在 50k steps 內未收斂 → near-greedy bad Q = worse than random |

**50k steps / 19 episodes 不足**：每個 episode 平均 ~2,600 env steps，共 19 個完整 episode。Q-function 見過的 job 配置組合太少，固定 alpha 後 near-greedy 策略強壓一個未收斂的 Q → JCT 比隨機更差。

### 4.5 Checkpoint 可用性驗證

```bash
from services.rl_scheduler.dsac import DSACAgent
agent = DSACAgent.load('runs/eval_dsac_fixed_alpha_20260514-201111/train/dsac.pt')
# → alpha=0.1000  log_alpha=-2.3026  fixed=True
# → select_action(obs, mask, greedy=True) 正常回傳 action index
```

serve.py（FastAPI `/act` endpoint）可直接載入此 checkpoint 做 shadow-mode deployment，行為與 §E 的 PPO 版本相同（policy 尚不可信，abstain rate 預計 100%）。

### 4.6 結論

1. **Score baseline 修正（epsilon=0.30 + bin-pack f_mps_fit）是本次 branch 最大的實質貢獻**，philly −24%，已 commit 且可立即部署。
2. **DSAC 需要更長訓練**。50k steps（19 episodes）遠不足以讓 Q-function 收斂並勝過手工調校的 score heuristic。預估需要 500k–1M steps 才有機會競爭。
3. **Infrastructure 完整**：MDP wrapper、DSAC agent、ReplayBuffer、sim training loop、FastAPI serve、live daemon、RLPD fine-tune scaffold 全部實作並測試通過（26 sim tests pass），具備繼續訓練的完整 pipeline。
4. **下一步（若繼續）**：以 `--total-steps 500000 --trace philly` 專一訓練，或啟用 GPU 大幅加速。

---

## 5. MLP vs Attention 架構比較

剛才確認 DSAC 在 fixed α=0.1 + 50k steps 下仍落後 score baseline。本節在 DSAC-attention branch 加入三項算法改進後，比較兩種 Q-network 架構的效果：

**算法改進：**

| 改進 | 說明 |
|------|------|
| n-step returns (n=10) | 預計算 10 步折扣回報再存入 replay buffer，降低稀疏 JCT reward 的 credit-assignment 延遲 |
| Score-guided warmup | warmup 期間以 score scheduler 選 action，取代 uniform random，提升 buffer 初始品質 |
| 短 episode (n_jobs=50) | 每集 50 個 job（原 100），同樣 steps 下產生約 2× 更多不同場景 |

**兩種 Q-network 架構：**

- **MLP**：twin Q + LayerNorm，obs → [256, 256] → n_actions（130k 參數）
- **Attention**：16 個 job token（11 維）→ TransformerEncoder（d=64, 4 heads, 2 layers）→ mean-pool → fuse cluster state → Q-head（78k 參數），permutation invariant

訓練設定：200k steps，CUDA，fixed α=0.1，UTD=4，n_jobs=50，seeds 42–46，philly/burst/ali 混合訓練。

---

### 5.1 平均 JCT 結果

| Family | MLP DSAC | Attention DSAC | Score baseline | MLP vs Score | Attn vs Score |
|--------|----------|----------------|----------------|-------------|---------------|
| philly | 4.815h | 5.396h | 2.621h | −83.7%\* | −105.9% |
| burst | 5.025h | 7.754h | 3.541h | −41.9%\* | −119.0%\* |
| ali | 1.991h | 3.952h | 1.383h | −44.0% | −185.9% |

`*` p < 0.05（paired t-test，n=5 seeds）。Δ 為負代表 DSAC JCT 高於 score（即 DSAC 較差）。

**MLP vs Attention 直接比較：**

| Family | MLP | Attention | Attention 相對 MLP |
|--------|-----|-----------|-------------------|
| philly | 4.815h | 5.396h | +12% 更差 |
| burst | 5.025h | 7.754h | +54% 更差 |
| ali | 1.991h | 3.952h | +98% 更差 |

---

### 5.2 Per-seed 分佈

**MLP per-seed JCT（小時）：**

| seed | philly | burst | ali |
|------|--------|-------|-----|
| 42 | 2.586 | 2.719 | 2.162 |
| 43 | 4.405 | 4.364 | 1.883 |
| 44 | 5.383 | 3.704 | 1.649 |
| 45 | 6.462 | 5.609 | 2.742 |
| 46 | 5.238 | 8.729 | 1.520 |

**Attention per-seed JCT（小時）：**

| seed | philly | burst | ali |
|------|--------|-------|-----|
| 42 | 3.966 | 2.664 | 7.703 |
| 43 | 6.904 | 4.153 | 2.192 |
| 44 | 4.160 | 9.212 | 1.892 |
| 45 | 1.967 | 10.885 | 3.616 |
| 46 | 9.984 | 11.857 | 4.359 |

Attention 的 burst 結果呈現明顯遞增趨勢（seed 44→45→46：9.2→10.9→11.9h），顯示高方差與潛在發散。

---

### 5.3 分析

**Attention 為何在此設定下表現較差：**

1. **Queue 規模太小**：16 個 job slot 對 TransformerEncoder 而言樣本數不足，attention 的 permutation invariance 優勢無法顯現；MLP 在小輸入下收斂更快。

2. **Transformer critic 較難收斂**：DSAC 的 TD 目標本身帶有 bootstrap 雜訊，Transformer 的多層 attention 在此雜訊下需要更多 steps 才能穩定；200k steps 對 MLP 已接近收斂邊緣，對 Attention 可能仍在早期。

3. **高方差**：Attention 各 seed 間的 JCT 差異（burst：2.7h 到 11.9h）遠大於 MLP（2.7h 到 8.7h），說明 Attention Q-net 對初始化敏感，訓練不穩定。

4. **MLP 的 LayerNorm + 深層結構**在 DSAC 的 actor-critic 框架下已被廣泛驗證（DrQ-v2、RLPD 等），是更成熟的 inductive bias。

算法改進（n-step、score warmup、短 episode）的貢獻難以單獨量化，因為 steps 從 50k 增至 200k、n_jobs 從 100 降至 50，兩者同步變動。但整體結果仍落後 score baseline，顯示核心瓶頸在於 reward 信號稀疏與 1×1 cluster 的訓練多樣性不足，並非架構本身。

---

### 5.4 結論

在當前 1×1 cluster（16-job queue）規模下：

- **MLP 架構優於 Attention**：更穩定、方差更小、各 family 均較好
- **Attention 需要更大 queue 或更多 steps** 才有意義（論文通常在 100+ job queue 下驗證）
- **兩種架構均未超過 score baseline** — 根本問題為訓練多樣性不足（1×1 cluster action space 太小）與 JCT reward 稀疏性

後續方向：(a) 增加 cluster 規模（2×2）擴大 queue + action space；(b) reward shaping 讓信號更密集；(c) RLPD 接入 live logs 提供真實分佈的 fine-tuning。

---

### 5.5 重現

```bash
# MLP（預設架構）
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --n-nodes 1 --gpus-per-node 1 --total-steps 200000 --n-jobs 50 \
    --trace-families philly burst ali --seeds 42 43 44 45 46 \
    --device cuda --no-attention

# Attention
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --n-nodes 1 --gpus-per-node 1 --total-steps 200000 --n-jobs 50 \
    --trace-families philly burst ali --seeds 42 43 44 45 46 \
    --device cuda
```

Artifacts：
- `runs/eval_mlp_20260514-210824/` — MLP 結果（checkpoint + CSV + JSON）
- `runs/eval_attn_cuda_20260514-232827/` — Attention 結果（checkpoint + CSV + JSON）

---

## 6. 六項改進組合測試（v3, 500k steps）

在確認 MLP 架構優於 Attention 後，v3 實驗將六項算法改進全部啟用並訓練至 500k steps：

**啟用的改進項目：**

| 項目 | 設定 |
|------|------|
| PER（優先經驗回放）| SumTree，α=0.6，β: 0.4→1.0 |
| Potential shaping | φ(s) = −Σwait_time / (scale × n_jobs)，γ=0.99 |
| CQL 正規化 | cql_alpha=0.1，抑制 Q 過估 |
| Curriculum | n_jobs: 10（前 20%）→ 30（30%）→ 50（後 50%）|
| n-step returns | n=10 |
| Score warmup | 前期以 score scheduler 填充 buffer |

訓練設定：500k steps，MLP 架構，CUDA，seeds 42–46，philly/burst/ali 混合訓練。

---

### 6.1 平均 JCT 結果

| Family | v3 DSAC (500k) | MLP DSAC (200k，§5) | Score baseline | v3 vs Score | 200k vs Score |
|--------|---------------|---------------------|----------------|-------------|---------------|
| philly | 6.295h | 4.815h | 2.621h | −140.2%\* | −83.7%\* |
| burst | 15.228h | 5.025h | 3.541h | −330.0%\* | −41.9%\* |
| ali | 6.727h | 1.991h | 1.383h | −386.5% | −44.0% |

`*` p < 0.05。Δ 為負代表 DSAC JCT 高於 score baseline（即 DSAC 較差）。

v3 在三個 family 上均比前一版 MLP (200k steps) **更差**，尤其 burst 出現嚴重衰退（15.2h vs 5.0h）。

---

### 6.2 Per-seed 分佈

**v3 per-seed JCT（小時）：**

| seed | philly | burst | ali |
|------|--------|-------|-----|
| 42 | 3.910 | 5.767 | 4.128 |
| 43 | 5.409 | 25.688 | 1.143 |
| 44 | 5.851 | 20.014 | 7.797 |
| 45 | 8.393 | 5.913 | 6.712 |
| 46 | 7.913 | 18.758 | 13.853 |

burst 的 seed 43/44/46 分別達到 25.7h / 20.0h / 18.8h，方差極大，顯示策略未收斂。

---

### 6.3 根因分析

**為什麼 v3 比 200k MLP 更差：**

1. **Curriculum 稀釋了目標環境的訓練量**。500k steps 的時間分配為：n_jobs=10 佔前 100k，n_jobs=30 佔中間 150k，n_jobs=50 僅佔最後 250k——實際上只有約 **24 個完整 episode**（每集 max_steps=10000）在目標規模下訓練，遠不足以讓 Q-function 對 n_jobs=50 的分佈收斂。

2. **CQL 在高方差獎勵下過度保守**。CQL 正則化懲罰高 Q 估計，在 score heuristic warmup buffer 的條件下，可能使策略過度依賴 warmup action 的分佈，抑制探索。burst trace 的 job 到達模式波動大，CQL 限制了策略對新情況的適應。

3. **Shaping + sparse reward 交互作用**。potential shaping 提供稠密信號（每步 φ 差分），但在 curriculum 的 n_jobs=10 小環境中校準的信號尺度，可能不符合 n_jobs=50 切換後的新環境，造成前期訓練的 Q-value 偏移難以修正。

4. **多干預同步啟用無法定位問題**。PER + CQL + shaping + curriculum 同時開啟，任何一個設定出問題都會互相掩蓋，且 500k steps 不足以讓這些機制在最終 n_jobs=50 分佈上充分交互學習。

---

### 6.4 結論

v3 實驗揭示了累積改進的陷阱：多項算法改進疊加並不保證效果相加，反而可能因 curriculum 設計與訓練量分配不當導致顯著衰退。

**關鍵教訓：**

- Curriculum 必須保證目標分佈（n_jobs=50）有足夠的 steps（建議 ≥ 200k）才能有效
- CQL 在 sparse + noisy 獎勵環境下的 alpha 值需要仔細調校（0.1 可能已過大）
- 改進應逐項 ablation 測試，而非一次全開

目前已確認的最佳設定為 **MLP + 200k steps + n-step + score warmup**（§5 結果），其中 burst 5.025h / philly 4.815h 是現有最佳 DSAC 效果，但仍落後 score baseline 約 40–84%。

根本瓶頸未變：**1×1 cluster 的 action space 太小、reward 信號稀疏**，下一步應擴展至 2×2 cluster 以提供更豐富的學習信號。

---

### 6.5 重現

```bash
PYTHONPATH=. .venv-m11/bin/python eval/scripts/eval_dsac_placement.py \
    --n-nodes 1 --gpus-per-node 1 --total-steps 500000 \
    --trace-families philly burst ali --seeds 42 43 44 45 46 \
    --device cuda --curriculum
    # PER + shaping + CQL=0.1 為預設，--curriculum 啟用 n_jobs ramp
```

Artifacts：`runs/eval_v3_20260515-092645/`（checkpoint + CSV + JSON）
