# Kubeflux — Scheduler Evaluation（Phase 6 M8 thesis chapter draft）

> 對應 thesis evaluation 章節。圖表 → [`eval/figures/`](../eval/figures/)；原始資料 → [`eval/results/`](../eval/results/)；完整 milestone 規格 → [`docs/scheduler.md`](scheduler.md)。
>
> 重現實驗：`bash eval/scripts/run_all.sh && .venv-m5/bin/python eval/scripts/plot_all.py`（sim）、`bash eval/scripts/e7_one_pass.sh <tag>`（live）。

---

## 摘要

Kubeflux 把 Slurm 跑在 Kubernetes 上做 GPU/MPS workload 排程，並用一組可調權重的 score function 取代 Slurm 內建 multifactor priority。本章驗證五件事：

1. **vendor backfill 已經做了大部分功**（FCFS 12.6h → multifactor 4.5h mean JCT，−72%），剩下空間有限。
2. **M5 runtime predictor 是真正的 win**：在三個 synthetic trace（philly、burst、ali）上跑 5 seeds × 3 family，paired same-seed 比較顯示 predictor 在有 contention 的 trace 上 statistically significant 改善 mean JCT 20–29%（CI 不跨 0）。
3. **M7 fragmentation reconciler 在所有三個 trace 上都是 net negative**（philly +33%、burst +61%、ali +6%）。這個 negative result 跨 distribution 一致，不是單一 trace artefact，且問題出在 victim 的 lost progress 大於排程改善。
4. **動態 weight tuning（M9 UCB1）能用 1/3 sim 預算找到接近 M8 grid 最佳的權重組合**。但因為 oracle vs 靜態最佳只差 2.5%，contextual tuning（LinUCB、PPO）沒有發揮空間。
5. **Live cluster 驗證 M3 score 部署無誤**，但因為 workload runtime 只跨 6× 範圍（vs sim 的 100×），predictor 的 ε·f_runtime_short 信號被 score 其他因子蓋過。Predictor 在 production 環境裡能不能發揮，取決於三件事：workload 本身夠 heterogeneous、predictor 在 matched distribution 上 retrain、score weights 配合 workload 跨度調整。少一個就看不到改善。

整套 evaluation 跨 465 sim runs（3 trace × 5 seed × 31 config）+ 4 live passes（vendor / our M3 / our_pred / hetero v2）+ 360 M9 bandit pulls，附 8 張 figure 加 cross-trace 比較。所有 raw data + 腳本 + 模型訓練流程都 commit 在 repo 內可重現。

---

## 1. 動機與貢獻

### 1.1 問題

用 Slurm 排程 HPC workload 已經是標準做法，但有兩個現實沒被內建 scheduler 直接處理：

1. **GPU 共用透過 NVIDIA MPS**。一張卡可切成 100 個 mps slot 給多個 job 同時跑。Slurm 預設 priority 不知道「mps:25 的小 job 應該比 mps:100 的大 job 先排」，所以容易讓一個大 job 卡住一堆小 job。
2. **Job runtime 不可預知**。同一個 user 提交相似 job 的實際時間可以差兩個數量級（幾分鐘到幾小時）。FCFS 跟 multifactor 都假設 runtime 不可知，SJF 反過來要求先知道誰短。

我們想回答的問題：**一個簡單可解釋的 score function 加上 runtime predictor，能不能在 GPU/MPS workload 上打贏 Slurm 預設？** 哪個因子貢獻最多、哪些子組件其實沒幫上忙？

### 1.2 我們做了什麼

1. **M3 五因子 score function**：`priority = α·f_mps_fit + β·f_vram_fit − δ·f_fragmentation + ε·f_runtime_short`。每個因子都是 [0, 1]，可解釋、可獨立 ablation。
2. **M5 runtime predictor service**：LightGBM 模型 + FastAPI service，從 sacct 歷史 train、預測 (user, gpu, mps, hour) → runtime。lua plugin 在 sbatch 時呼叫。
3. **M7 fragmentation reconciler**：Slurm 沒看到的死角——當 head pending job 被卡住而某個低 priority running job 釋放就能放它過去時，主動 requeue 那個 victim。Operator 端做 detect → decide → actuate，預設 shadow mode。
4. **M8 evaluation 基建**：discrete-event simulator 跑三個 trace family、5 seeds 每組、paired CIs；live cluster 4 個 pass 驗證部署。
5. **M9 weight tuning**：UCB1 + LinUCB 在 sim 上比靜態 grid search 省 sim 預算。
6. **Operator hardening**：根據 live 實驗踩到的 wedge 狀態，加 ghost-job detector + 對應 Prometheus alert。

### 1.3 主要結論一覽

| Claim | 證據 | 狀態 |
|---|---|---|
| C1 vendor backfill 已吃掉大部分改善空間（FCFS → multifactor −72%）| sim cross-trace | ✅ |
| C2 純 M3 score 不顯著改善 mean JCT | E2 vs E3 paired ≈ 0 | ✅ |
| **C3 M5 predictor 在 contention-heavy trace 上 −20.1%（philly）/ −28.7%（burst）, CI 不跨 0** | E4 vs E2 paired | ✅ statistically significant |
| C3b Predictor 在 sparse trace 上沒效（ali −0.08%，util 才 0.30）| E4 vs E2 on ali | ✅ |
| **C4 M7 fragmentation 在三個 trace 上都 net negative** | paired diffs all CI > 0 | ❌ negative result |
| C5 Score weight sensitivity 跟 contention pressure 正相關（ali 0.1%、philly 10.6%、burst 28.5%）| E6 5×5 grid | ✅ |
| C6 UCB1 用 120 sim runs 拿到 +3% vs M8 grid-best 的 375 runs | M9 結果 | ✅ |
| C7 Live cluster 驗證部署正確，但 e7 workload 跨度不足以讓 predictor 發揮 | 4 個 pass | ✅ scoped |

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

### 2.2 Predictor

Service 是 FastAPI + LightGBM。Features 取自 `job_desc`：`user_name`、`gpu_count`、`mps_req`、`gpu_type`、`hour_of_week`、`user_freq`、`user_mean_log_rt`。後兩項從歷史 sacct rolling 統計來——所以新 user 會 fallback 到 bootstrap mode（回 `min(user_time_limit, 4h)` 常數）。

lua plugin 用 `curl --max-time 200ms` 呼叫，response timeout / 解析失敗都安全降級（不動 priority）。

### 2.3 Fragmentation reconciler

Operator 每 15 秒 poll slurmrestd：

1. **Detect**：取 pending head 跟所有 running job。
2. **Decide**：head 卡住 ⇔ 沒有單一 node 容得下，但若 release 某個低 priority running job 就能容下時，挑那個 victim。需通過 `FRAGMENTATION_PRIORITY_GAP` 跟 `FRAGMENTATION_MAX_REQUEUES_PER_HOUR`。
3. **Actuate**（shadow mode 時只 log；live 才 `scontrol requeue`）。

### 2.4 Architecture（簡圖）

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

### 3.1 為什麼用 sim + live 兩條腿走

Sim 跟 live 各補對方的盲點：

- **Sim** 能跑大量 seed × trace 拿 paired CIs，可以負擔 weight sensitivity grid、negative result 跨 trace 驗證。缺點是 predictor 假設 RMSE=0、cluster 沒 instability、checkpoint reload 是參數設的（不是真實測量）。
- **Live cluster** 拿單卡 RTX 4070 + k3s 驗證「整套 stack 在真機跑得動」、看 lua plugin / netpol / image 等 deployment caveat。缺點是 workload 規模上不去，infrastructure noise (controller restart、AllocTRES zombie、worker NOT_RESPONDING) 在這個尺度上常常 dwarf scheduler signal。

兩邊結果交叉檢查時會碰到一個落差：sim 在 heterogeneous trace 上看到 predictor 顯著 win，live 同條件下看不到。這個落差本身需要解釋，§5 跟 §7 給出 3 條件 framework。

### 3.2 Trace families（sim）

三個 synthetic generator 都在 `sim/loader.py`：

| Family | 特色 | 用來測什麼 |
|---|---|---|
| `philly` | Poisson arrival、log-normal runtime（median ~30 min, p95 ~6h）、~75% 單卡、30% 單卡 job 是 MPS-fractional | baseline workload，跟 Microsoft Philly trace shape 近似 |
| `burst` | 同 job-size mix，arrival 集中在每 6h 一次的 2h 高峰窗 | 高 contention，測 scheduler 在排隊壓力下的行為 |
| `ali` | Alibaba PAI-like：90% 單卡、median runtime ~13 min、60% 單卡 job MPS-fractional、晝夜節律 | 短 job 為主、低 util，測 scheduler 在沒 contention 時是否會 over-engineer |

每 family 跑 5 seeds（42–46），用 paired same-seed diff 做主要比較。Paired diff 把「不同 seed 帶來的 trace 變動」消掉，CI 比 unpaired 緊一個量級。

### 3.3 實驗矩陣

| 編號 | scheduler | 額外 flag | 對應 milestone |
|---|---|---|---|
| E1 | FCFS（無 backfill）| — | baseline (worst) |
| E2 | multifactor + backfill | — | Slurm 預設 |
| E3 | score (M3) α=0.40, β=0.20, δ=0.20, ε=0 | — | M3 完成度 |
| E4 | score + predictor (M5), ε=0.30 | — | M5 邊際價值 |
| E5 | E4 + M7 fragmentation, 60s ckpt | `--fragmentation --ckpt-reload-cost 60` | M7 邊際價值 |
| E5b | E5 但 ckpt cost=0 | `--ckpt-reload-cost 0` | M7 樂觀 upper bound |
| E6 | E4 sweep 25 (α, δ) cells | 5×5 grid | sensitivity |

E1–E6 在 sim 跑（3 traces × 5 seeds × 31 configs = 465 runs）。E7 是 live cluster 4 個 pass：vendor、our (M3 only)、our_pred (M3+M5)、hetero_v2 (M3+M5 with retrained predictor)。

### 3.4 兩個方法論 bug，修了以後結果完全改寫

中途發現舊版 evaluation 的數字不可信，記錄修法：

1. **Sim victim JCT bug**：`try_fragmentation_reconcile` 把 victim 的 `metrics.submit_ts` reset 成 `now`，導致 JCT 只算重排後那段——低估了原始排隊時間，讓 M7 看起來假性 win。修法：保留原始 submit_ts，scheduler 看到的 vj.submit_ts=now 不變（fairness 應該如此）。
2. **單一 deterministic sample**：原本每個 cell 只跑一次。改成 5 個 seed，所有指標報 mean ± std 加 paired 95% CI。

加碼：
3. **Checkpoint reload cost 沒模型**：加 `--ckpt-reload-cost`（預設 60s）給 fragmentation 模式用。

這三個修正前後對比寫進 `docs/note.md` Debug Record #15。**舊版「E5 −28.6% vs vendor」是上述 bug 加單 sample 造成的人為結果，新版被推翻**。

---

## 4. Simulator 結果

### 4.1 主表（5 seeds 每 cell，三個 trace）

#### philly（baseline workload）

| exp | jct_mean (h) | jct_p90 (h) | slow_mean | util | bf_rate | requeues/run |
|---|---:|---:|---:|---:|---:|---:|
| E1 FCFS | 15.78 ± 12.44 | 29.32 | 77.94 | 0.742 | 0.000 | 0 |
| E2 multifactor+bf | 4.49 ± 2.60 | 9.49 | 17.68 | 0.812 | 0.815 | 0 |
| E3 score (M3) | 4.71 ± 3.18 | 10.83 | 19.39 | 0.808 | 0.842 | 0 |
| **E4 + predictor (M5)** | **3.43 ± 1.61** | **6.35** | **10.04** | 0.805 | 0.854 | 0 |
| E5 + frag (M7), 60s ckpt | 4.55 ± 2.12 | 9.47 | 14.32 | 0.839 | 0.948 | 349 |
| E5b M7, 0s ckpt | 4.20 ± 1.70 | 9.18 | 12.34 | 0.827 | 0.950 | 347 |

#### burst（高峰擠壓 workload）

| exp | jct_mean (h) | jct_p90 (h) | slow_mean | util | bf_rate |
|---|---:|---:|---:|---:|---:|
| E1 | 17.36 ± 10.71 | 29.38 | 76.15 | 0.708 | 0.000 |
| E2 | 6.36 ± 4.35 | 12.85 | 25.91 | 0.768 | 0.861 |
| E3 | 6.62 ± 4.37 | 16.54 | 26.88 | 0.766 | 0.896 |
| **E4** | **4.24 ± 2.10** | **8.69** | **12.76** | 0.764 | 0.906 |
| E5 | 6.64 ± 3.15 | 14.54 | 20.20 | 0.798 | 0.970 |
| E5b | 6.96 ± 4.72 | 17.14 | 21.18 | 0.784 | 0.966 |

#### ali（短 job、稀疏 workload）

| exp | jct_mean (h) | jct_p90 (h) | slow_mean | util |
|---|---:|---:|---:|---:|
| E1 | 0.515 ± 0.038 | 1.101 | 1.045 | 0.305 |
| E2 | 0.512 ± 0.037 | 1.098 | 1.026 | 0.305 |
| E3 | 0.512 ± 0.037 | 1.098 | 1.025 | 0.305 |
| E4 | 0.512 ± 0.037 | 1.099 | 1.018 | 0.305 |
| E5 | 0.543 ± 0.050 | 1.127 | 1.059 | 0.321 |

ALI 上所有 scheduler 結果幾乎一致——util 只有 30%，根本沒有排隊壓力給 scheduler 表現空間。**這是 contention 的負面對照組**。

### 4.2 Paired same-seed 比較（重點）

| trace | E4 vs E2（predictor 邊際）| E5 vs E4（M7 邊際） | E5 vs E2（整套 stack） |
|---|---:|---:|---:|
| philly | **−20.06%** ± 11.95% ↓ | **+33.08%** ± 5.22% ↑ | +6.31% ± 15.42%（不顯著）|
| burst | **−28.71%** ± 11.31% ↓ | **+60.95%** ± 31.71% ↑ | +14.85% ± 30.51%（不顯著）|
| ali | −0.08% ± 0.09%（不顯著） | **+6.03%** ± 4.48% ↑ | +5.95% ± 4.44% ↑ |

↓/↑ 表示 95% CI 不跨 0（statistically significant）。

兩個 takeaway：

1. M5 predictor 在 contention-heavy 兩個 trace 上效應顯著且大（−20% / −28%）。ali 上不顯著是因為 cluster 連塞滿一半都沒有，predictor 沒有作用空間。
2. M7 fragmentation 在三個 trace 上一致 net negative、CI 都不跨 0。這個 negative result 跨 distribution 一致，是設計問題不是 philly artefact（§7 詳論）。

### 4.3 圖表敘事

完整 8 張圖在 `eval/figures/`。每張的故事：

- **fig1 JCT bars (mean/p90/p95 with 95% CI error bars)** — E1 那根壓著其他四個一個量級，所以章節真正要看的差距在 E2..E5 之間。E4 三根都明顯比 E5 矮、error bar 不重疊。

- **fig2 JCT CDF (log x)** — E1 的 CDF 在低 x 處遠左，意思是連最快的 50% job 都比其他組慢一個量級。E4 整段壓在 E5 下方，p50/p90/p95 全勝。

- **fig3 Slowdown box** — E1 的 box 落在 [10, 100]，E4 收到 [1, 10] 左右，IQR 縮了一個量級。E5 的 box 比 E4 高一截，反映 requeue 後重做 lost progress 對短 job 的 slowdown 影響特別大。

- **fig4 Utilisation timeline** — E5 比 E4 utilization 高一點點，但這部分高出來的工作量是「重做被 evict 的 job」，是 phantom utilization，不對應到 throughput 改善。

- **fig5 E6 5×5 heatmap** — philly 上 jct_mean 範圍 3.28–3.63h，spread 10.6%；burst 28.5%（重壓力下 weight 比較敏感）；ali 0.1%（沒 contention 就沒差）。**最佳 cell 在 α≤0.25、δ≤0.15**——把 mps_fit 拉低、fragmentation penalty 別太大。

- **fig6 backfill rate + requeue count** — E5/E5b bf_rate 衝到 0.95、requeue/run ~349–356（philly/burst）/ 92（ali）。看起來 reconciler 把節點塞滿，但塞進去的有相當比例是被踢掉後重排的 job，這就是 fig4 phantom utilization 的根源。

- **fig7 normalised improvement vs E1** — philly 上 E4 −78%、E5 −71%（E5 比 E4 退步），其他兩條同樣是 E4 最深、E5 反彈。

- **fig8 cross-trace generalisation** — 兩組 paired Δ 三個 trace 並排：綠色 predictor 在 philly/burst 顯著往下（−20% / −29%），ali 趴在 0；紅色 M7 三根都在 0 以上 CI 不跨 0。**M7 negative result generalises across distributions**。

### 4.4 為什麼 M7 是 net negative

仔細看數字背後的機制。E5 平均每 run 350 次 requeue，每次 requeue 都把 victim 從零重跑（lost progress）。即使 E5b 把 ckpt reload cost 設 0（樂觀 upper bound）、純看 lost-progress 的影響，philly 上 E5b 仍比 E4 差 22%、burst 差 24%。**問題本質不在 reload overhead，而在 victim 整段重跑**。

當前 M7 victim selection 純看 priority——找最低 priority 的 running job 踢掉。這個策略對「已執行很久的 victim」沒有抵抗力：一個跑了 90% 完成度的 job，被踢掉重跑要付出整段成本，但釋放的 mps slot 只夠 head job 跑 10% 的時間。Trade off 在這份 workload distribution 上不划算。

Future work 方向（fix C4 negative result）：

1. Victim selection 加「已執行時間 / 完成比例」penalty——別踢已經跑完一半的 job。
2. 改 preempt + suspend，保留 GPU memory state，把 lost progress 從 100% 降到接近 0。
3. 只在 predictor 估計 head 剩餘時間 < victim 剩餘時間時觸發。

這幾個方向其中之一試出來能讓 paired diff 翻正，就是 C4 從「flat negative result」升級成「conditional win」。

---

## 5. Live Cluster 結果

### 5.1 Cluster 規模與限制

| 項目 | 值 |
|---|---|
| Cluster | k3s + 1 × RTX 4070（NVIDIA device plugin time-slicing 切成 2 個 logical GPU、2 worker pod 共享同一塊實體卡，每 pod 配 100 mps slot in slurm.conf）|
| Workload | 20 jobs：12 短 (mps:25, 60–120s) + 6 中 (mps:50, 180–300s) + 2 大 (mps:100, 240–360s) |
| 預期 wall-clock | optimal ~24 min；實測 25–50 min per pass（含 cold start + 偶發 cluster wedge） |
| 數據來源 | `sacct` 從 controller pod 直接取（login pod 連 slurmdbd 不穩，§7 提）|

跟 sim 比規模天差地遠（sim 是 16 等效 GPU、1000 jobs），所以**live 數字不是用來複現 sim 量化結論，是用來證明「整套 stack 在真機跑得動」、找出 deployment caveat**。

### 5.2 四個 pass 的數字

| Pass | mean JCT | vs vendor (paired) | vs prev (paired) | 改善 job 數 |
|---|---:|---:|---:|---:|
| vendor (multifactor only) | 932.6s | — | — | — |
| our (M3 score) | 394.5s | **−57.70%** | — | 18/20 |
| our_pred (M3+M5, bootstrap predictor) | 394.0s | **−57.75%** | vs our: −0.13% | 20/20 |
| our_pred_hetero (5 user buckets) | 766.4s | −17.83% | vs our_pred: +94.52% ↑ | 5/20 |
| **our_pred_hetero_v2 (matched predictor)** | **393.7s** | **−57.79%** | vs v1: −48.63% ↓ | 18/20 |

### 5.3 五個 caveat 與 takeaway

讀上面數字之前要先把這幾個東西放在心上：

1. Vendor pass 中段卡了 30 分鐘的 cluster freeze。一個 mps:100 job 卡 COMPLETING、AllocTRES 殘留 50 slot 沒釋放，我手動 `scontrol reconfigure` 救活。所以「vs vendor −57%」這個 headline 有相當比例不是 scheduler win，是「對方塞車不見」。乾淨子集（small jobs，cluster 已暖機）的 paired diff 是 **−38.6%**，這個才適合放進論文 main result。
2. our_pred vs our 差 −0.13%。同 cluster 條件、只切 predictor 開關，predictor 沒帶來邊際改善。直接的解釋是 root user 沒有歷史，predictor 對所有 job 回類似的常數。
3. hetero v1 vs our_pred 差 +94%。看似 predictor 反向傷害，但拆 per-job wait 後發現 96% 的差距來自 v1 那次撞到的 cluster wedge（約 17 min freeze），不是 scheduler 行為。
4. hetero v2 vs our_pred 差 −0.08%。把 v1 的 cluster issue 排除、predictor 用 distribution-matched 模型重訓、verified 對不同 user 回不同預測（u05=87s、u34=242s）後，predictor 仍然沒帶來改善。
5. 在小規模 cluster 上，infrastructure noise 大於 scheduler signal。一次 controller restart 可以讓某個 job 多等 7 分鐘；20 個 job 的 workload total wall clock 也才 25 分鐘。

### 5.4 為什麼 v2 retrained predictor 還是沒幫上？

這是整個 live evaluation 最值得寫的故事。

#### 假設與測試

`v1 → v2` 的修法基於一個 plausible hypothesis：**bootstrap predictor 是 train on Philly synthetic（median runtime 30 min），但 e7 workload 跑 60–360s，差 5–10×。模型對 u20 job 預測 766s、實際只跑 60–120s。Predictor 看到的世界跟 cluster 真實發生的事完全脫鉤，自然幫不上忙。**

比喻一下：就像找一個只熟 marathon 配速的教練來指揮 100 公尺短跑——他喊出來的「保留體力、慢一點」對短跑沒用，甚至誤導。

修法三步：
1. 生 1500-sample trace，5 個 user 各對應一個典型 runtime（u05=90s、u20=106s、u01=167s、u10=229s、u34=257s——刻意做出 3× spread）。
2. 重訓 lgbm，MAE_log 從 1.23 降到 0.34（multiplicative error 從 5–10× 收到 1.4× 上下）。
3. `kubectl cp` 進 PVC + restart predictor pod。Probe `/predict` 驗證 u05 → 87s、u34 → 240s——預測值都落在實際 workload 範圍裡。

#### 一個額外的踩坑：predictor 也會改 walltime

第一次跑 v2 pass，**5/20 jobs TIMEOUT**。Predictor wiring 預設 `applyTimeLimit=true`，意思是 predictor 算出多長就把 Slurm `--time` 改寫多長。新模型對 u01 預測 ~150s、但實際 medium job 跑 200–300s，Slurm 在 150s 把它砍掉。

修法：chart 切 `slurm.jobSubmit.predictor.applyTimeLimit=false`，只讓 predictor 做排序、不要管 walltime。再跑：0 TIMEOUT。

#### 真正的結果

| 比較 | Δ mean JCT | 改善 job 數 |
|---|---:|---:|
| v2 vs vendor | **−57.79%** | 18/20 |
| v2 vs v1（mismatched predictor）| **−48.63%** | **20/20** |
| v2 vs our_pred (homogeneous) | **−0.08%** | 10/20 |

第二列看似爆改善但其實是 v1 那次的 cluster freeze 不見了。**第三列才是真正乾淨的對比**：同 workload、同 cluster 條件、只差 predictor 模型品質——paired diff 只有 −0.08%，在 noise 內。

#### 為什麼？因為 score function 給 predictor 的權重太小

公式回顧：`f_runtime_short = horizon / (horizon + pred_runtime)`，horizon=3600s。新模型對 5 user 的預測 spread：

| user | pred (s) | f_runtime_short |
|---|---:|---:|
| u05 | 87 | 0.976 |
| u20 | 102 | 0.973 |
| u01 | 108 | 0.971 |
| u10 | 218 | 0.943 |
| u34 | 240 | 0.937 |

`f_runtime_short` 最大 spread = 0.976 − 0.937 = **0.039**。乘 ε=0.30 = 0.012 priority unit 差距。再乘 `scoreGain=1000` = **12 priority points**。

Slurm 的 `PriorityWeightAge=1000` 設定下，等 1 小時 = 1000 priority points。**Predictor 提供的全部信號 = 43 秒的等待換算值**。對 e7 workload（job 排隊只等幾分鐘）這完全淹沒在 noise 裡。

要 predictor 撼動排序，需要 workload 裡 job 長度跨好幾個量級。Sim Philly trace 跨 100×（60s 到 6h），對應 f_runtime_short 從 0.99 到 0.37，spread 0.62——比 e7 大 16 倍。這就是為什麼 sim 看到 −20% 而 live 看不到。

### 5.5 Predictor 發揮的三個條件

Predictor 的價值取決於 workload。三件事要同時成立才會看到改善：

1. Workload 本身要 heterogeneous，job 長度跨多個量級，不是「都差不多 200 秒上下」。
2. Predictor 要在這份 distribution 上 train。v2 做到這項，但只解決條件 2 不解決條件 1。
3. Score function 的 ε 跟 horizon 要跟 workload 跨度配。當前預設 ε=0.30、horizon=3600 對 e7 的 6× 跨度太保守。

少一條都不會看到效果。Production 部署前要先看 sacct 跨度，一個只跑 micro-benchmark 的 cluster 不會從 M5 拿到改善。

### 5.6 Cluster instability：一個獨立的 thesis-worthy 觀察

E7 過程踩到的 wedge 狀態詳細記在 `docs/note.md` #16。六層交互：

1. helm post-upgrade hook (`gpu-labeler`) 撞 BackoffLimitExceeded → 用 `--no-hooks` 繞
2. Controller restart → workers NOT_RESPONDING → in-flight jobs 卡 COMPLETING（Slurm 21.08 對 controller 頻繁重啟沒做好）
3. Operator 把 ghost COMPLETING jobs 當 running、拒絕 scale up → 死結
4. AllocTRES zombie：mps slots accounted 但實際沒 job 在跑（Slurm 21.08 GRES accounting bug；`scontrol reconfigure` 救）
5. `sacct` 從 login pod 連 slurmdbd 失敗、controller pod 卻 OK（netpol / munge socket）
6. device plugin time-slicing 把一塊實體 GPU advertise 成 2 個——Slurm 排程 200 mps slot，實際吞吐 100 slot 等級

針對 #3 我們做了 `operator/ghost_detector`：偵測 `current_replicas==0 && pods==0 && running_jobs>0` 並發 `ghost_jobs_detected` 事件 + Prometheus `GhostJobsWedge` alert。SOP 寫在 note.md §16.7。

這部分是 thesis 的次要發現——**production-quality scheduler 評估需要的最小 cluster size 大於我們手上的單卡 setup**。Wedge events 約 10 分鐘等級、加總常與整個 workload 同等量；要量到 scheduler 的純效果，要嘛 cluster 更穩、要嘛 workload 拉到幾小時讓 noise 平均掉。

---

## 6. M9 動態 Weight Tuning（純 sim）

### 6.1 問題

M8 的 E6 5×5 sensitivity grid 證明 weight 對 mean JCT 的最大 spread 是 burst 28.5% / philly 10.6% / ali 0.1%。M9 問的不一樣：**有沒有比窮舉 grid 更省 sim runs 的方式找到接近最佳的 weight？**

### 6.2 設定

- Arm 空間：(α, δ, ε) 在 3³ = 27 個 tuple 上掃。β=0.20 固定。
- Context 空間：每個 (trace, seed) 算一個 3-dim feature `(n_jobs/2000, mean_mps/4, mean_gpu/8)`。三個 family × 5 個 seed = 15 個 context。
- Reward：`−jct_mean / 3600`（負時、越大越好），由 `sim.runner.run` 跑單次得到。
- 三個 policy：`random`（uniform）、`UCB1`（非 contextual）、`LinUCB`（每 arm 一個 ridge regression head, α=0.6, ridge=1.0）。
- 預算：120 個 training round（vs M8 grid 用 375 sim runs，3×5×25）。

實作在 `services/weight_tuner/{bandit.py,sim_env.py}`，driver 是 `eval/scripts/run_m9_linucb.py`。

### 6.3 結果

| Policy | eval mean JCT (h) | vs M8 grid-best | sim runs |
|---|---:|---:|---:|
| random baseline | 3.217 | +28.1% | 120 |
| **UCB1** | **2.587** | **+3.0%** | 120 |
| LinUCB | 2.745 | +9.3% | 120 |
| M8 grid-best (static) | 2.511 | — | 375 |
| Oracle (per-context) | 2.448 | −2.5% | full |

對應 `eval/figures/fig9_m9_regret.png` 的 cumulative regret 曲線：UCB1 在 ~30 round 後 regret 成長近乎打平、LinUCB 緩慢一點、random 線性成長。

### 6.4 三個直接結論

1. **Oracle 跟 M8 grid-best 只差 2.5%**——以這個 workload 池來說，一個固定 arm 已經把 weight tuning 能拿的近乎全部抓掉。Per-context 細調的 headroom 上限只有 2.5%。
2. **UCB1 用 1/3 樣本拿到 +3% 的答案**——M8 跑 375 次 sim 求 grid optimum，UCB1 只用 120 次就到 2.587 h、比 M8 grid-best 多 3%、比 oracle 多 5.7%。**Sample efficiency 才是 M9 的真價值**。
3. **LinUCB 不如 UCB1**——因為 oracle vs static best 只差 2.5%，context 提供的信號很弱、ridge regression 反而被 fit noise 拖累。**Contextual tuning 在這個 workload mix 上沒意義**。

### 6.5 為什麼不做 PPO

原 M9 spec 寫 LinUCB + PPO。LinUCB 結果已經對「contextual 在這個問題上不值得」做了定量回答：oracle 跟 static best 差 2.5%、context 不能挽救這個天花板。PPO 是更貴的同類型方法、不會超越 oracle、最多在 sample efficiency 上略好。以 thesis ROI 看不值得。

要 deploy 的話 UCB1 就夠——cold start 100 個 sbatch 後 weight 就 converge。但 M9 不需要 deploy 就能寫進 thesis：純 sim 數字已經回答「動態 tuning 值不值得」的問題。

---

## 7. 討論

### 7.1 評估方法上的 design choice

1. Paired same-seed + 5 seeds + Student-t 95% CI 是主要量化工具，E5b 對照組用來獨立估 ckpt cost 的成分。中途發現 submit_ts reset bug、整個 M7 結論翻盤，這個過程說明為什麼 cross-seed 變異需要從一開始就做。
2. M7 的 net negative 結果跨三個 distribution 一致呈現。文中沒有把它包裝成「邊際 win」，而是試著精確定義「victim selection 純看 priority 在哪些 workload pattern 下失敗」，並列出三個可驗證的修法方向。
3. Sim 跟 live 在 M5 predictor 結果上不一致時，我們選擇建立一個能跨兩邊都成立的解釋（§5.5 三條件 model），而不是只報其中一邊的數字。

### 7.2 哪些地方可以做更好

1. **Live cluster 規模不夠**：單卡 RTX 4070 + 20-job workload 上 infrastructure noise 跟 scheduler signal 同量級，量化 predictor 的 live 效果做不到。需要多卡 cluster 或更長 workload。
2. **Predictor 訓練資料假設**：sim E4 假設 RMSE=0（給 predictor 真實 runtime），live e7 用 1500-sample synthetic 替代。Production 部署要用 cluster 自己的 sacct 歷史 retrain，這部分沒在這個 thesis cycle 內驗證。
3. **M7 沒實作改良版**：identifying victim-selection 是 root cause 但沒實作 elapsed-progress penalty / preempt+suspend / predictor-conditioned trigger 三個 fix 的任何一個。Negative result 留在「我們知道為什麼會失敗」階段，不到「我們知道怎麼修」。

### 7.3 對 production 部署的建議

如果有人想把 Kubeflux 部署到實際 cluster，這些是該看的東西：

- **打開的東西**：score function（M3，三因子預設權重 robust）、predictor service（M5，前提是有 sacct 歷史 retrain）。
- **關閉的東西**：fragmentation reconciler（M7 預設 `shadowMode=true`，等 victim selection 改進前不要 `live`）。
- **要監控的東西**：operator `ghost_jobs_present` gauge（cluster wedge 偵測）、`bf_rate`（backfill 是否還在 work）、`predict_total{mode}`（predictor 服務是否被 lua 呼叫到）。
- **要 retrain 的東西**：predictor cronjob 每天或每週從新 sacct 跑一次（chart 已內建 `runtimePredictor.retrain.enabled`）。

---

## 8. 限制與威脅效度

- **三個 trace 都是 synthetic**：philly / burst / ali 是三組不同 distribution 的 generator。已經跨 distribution 驗證 negative result，但真實 production trace（Helios、Alibaba 公開資料）可能呈現不同 contention 結構。換 `--trace` 參數就能 repro。
- **Lost-progress 假設保守**：sim 讓 victim 從零重跑，沒模擬 partial checkpoint resume。E5 已加 60s reload overhead，但「已完成的 work 100% 重做」這個假設偏悲觀。改 preempt+suspend 可大幅縮小。這也是 §4.4 future work 方向 2。
- **Predictor 假設完美（sim）**：sim 裡 `f_runtime_short(true_runtime)` 等於假設 RMSE=0。實測 RMSE 約 ±20%（services/runtime_predictor 訓練輸出），真實環境 E4 改善幅度應該打 8 折。
- **mem / IO 沒模擬**：sim 只追蹤 MPS slot 跟 GPU 數，GPU job 的 IO wait 也是 JCT 顯著貢獻者，在 sim 裡假設「已內含於 runtime」。
- **E6 grid 升級到 5×5，但仍沒掃 β 跟 ε**：要進一步聲明 weight robustness 需要 full 4-D scan。
- **Live cluster 單 GPU**：單卡 + device plugin time-slicing 把 1 個實體 GPU 切成 2 個 logical，Slurm 視 200 mps 為獨立資源但實際吞吐量是 100 mps 級。Production multi-GPU cluster 行為會不同。
- **Live workload 規模**：20 jobs / 25 min wall clock，跟 cluster wedge 事件等量級。要量到 scheduler 純效果需要更長 workload。

---

## 9. 結論與未來工作

### 9.1 核心結論

整套 stack 裡，M5 runtime predictor 是效應最確定的元件。Sim 上 philly −20%、burst −29% mean JCT，paired CI 都不跨 0；ali 因為 cluster 沒 contention 所以看不到差。

M7 fragmentation reconciler 在當前 victim-selection 設計下是 net negative。三個 trace 結果一致，且 ckpt reload cost 不是主因，victim 整段重跑造成的 lost progress 才是。Negative result generalises 跨 distribution。

Live cluster 部分證明部署沒問題，但量化效應做不到。單卡 RTX 4070 加 20-job workload 上，infrastructure noise（controller restart、AllocTRES zombie）跟 scheduler signal 處在同一個 wall-clock 量級。要在 live 看到 sim 等級的改善，得同時滿足三件事：workload runtime 跨多個量級、predictor 在 matched distribution 訓練、score function 的 ε/horizon 配合 workload 跨度。

M9 UCB1 在 sim 上用 1/3 預算（120 vs 375 runs）找到接近 M8 grid-best 的 weight。Oracle 跟 static best 差只有 2.5%，所以 contextual tuning（LinUCB、PPO）沒有發揮空間。M9 的價值是 sample efficiency 而不是 contextual adaptation。

### 9.2 Future work（按優先級）

1. M7 victim selection 改良。加 elapsed-progress penalty 或改 preempt+suspend，試試能不能把 +33% paired diff 翻成 conditional win。一天工期，成功的話 C4 可以從 negative result 升級成「找到讓 M7 work 的條件」。
2. Multi-GPU cluster live re-run。拿到 2 張以上實體卡的 cluster 後再跑 E7，量化 predictor 的真實 live 效果。預估需要 50 jobs 以上、workload runtime 跨度 10× 以上才能蓋過 infrastructure noise。
3. Predictor 在線 retrain pipeline 長期驗證。chart 已內建 cronjob 但沒長期跑過，需要 4 週 production sacct 才能觀察 MAE_log 趨勢跟 stale model fallback 行為。
4. 真實 production trace 替換。拿 Alibaba 公開 trace 取代 synthetic generator，看 cross-trace 結論是否一致。
5. Score function 4-D weight scan。目前 5×5 只掃 (α, δ)。完整 (α, β, δ, ε) 四維可能找到更好 corner。

---

## A. 重現步驟

```bash
# 0. 第一次跑 — 裝 venv
uv venv .venv-m5 && uv pip install --python .venv-m5/bin/python pytest matplotlib lightgbm

# 1. Sim E1..E6 全跑（< 5 分鐘）
bash eval/scripts/run_all.sh

# 2. 出圖（< 10 秒）
.venv-m5/bin/python eval/scripts/plot_all.py

# 3. 看主表 + paired diff
.venv-m5/bin/python eval/scripts/print_summary.py

# 4. M9 bandit experiment（< 3 分鐘）
PYTHONPATH=services .venv-m5/bin/python eval/scripts/run_m9_linucb.py

# 5. （optional）E7 live cluster — 需要 kubeconfig 指到 chart 部署好的 k3s
bash eval/scripts/e7_one_pass.sh vendor        # pass A
helm upgrade ... --set slurm.jobSubmit.enabled=true
bash eval/scripts/e7_one_pass.sh our           # pass B
.venv-m5/bin/python eval/scripts/e7_compare.py eval/results/e7/vendor.csv eval/results/e7/our.csv
```

可調整的環境變數：
- `TRACES="philly burst ali"` 控制 sim 要跑哪幾種 trace
- `SEEDS="42 43 44 45 46"` 控制 seed 列表
- `E6_GRID=DENSE` (5×5) 或 `SMALL` (3×3 legacy)
- `CKPT_COST=60.0` E5 的 checkpoint reload cost

Raw artifacts：
- `eval/results/<trace>/<exp>/<run>__seed<N>.{csv,json}` 每個 seed 的 per-job + summary
- `eval/results/all_summaries.json` 全部 465 runs 攤平
- `eval/results/agg_by_run.json` 跨 seed 聚合的 mean / std / 95% CI
- `eval/results/e7/{vendor,our,our_pred,our_pred_hetero,our_pred_hetero_v2}.csv` live sacct
- `eval/results/m9/m9_history.csv` + `m9_summary.json` bandit 數據
- `eval/figures/fig{1..9}.{png,pdf}` 圖

---

## B. 驗收

- [x] 跨 trace raw data 齊全（3 traces × 5 seeds × 31 configs = 465 sim runs）
- [x] 9 張圖（fig1 bars、fig2 CDF、fig3 box、fig4 util、fig5 heatmap、fig6 bf+requeue、fig7 normalised、fig8 cross-trace、fig9 M9 regret）
- [x] M7 negative result 跨 distribution 驗證
- [x] E7 live cluster 4 個 pass 完成（vendor / our / our_pred / hetero_v2）
- [x] M5 predictor live deployment + 端到端 wiring 驗證
- [x] M9 LinUCB / UCB1 sample efficiency vs M8 grid 比較
- [x] Operator hardening：ghost-job detector + GhostJobsWedge alert
- [x] eval-writeup.md 9 個章節 + 2 個 appendix（本檔）
