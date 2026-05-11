# Phase 6 M8 — Evaluation Writeup

> 對應 thesis evaluation 章節草稿。
> 圖表來源：[`eval/figures/`](../eval/figures/)，原始資料：[`eval/results/`](../eval/results/)

> 重現：`bash eval/scripts/run_all.sh && .venv-m5/bin/python eval/scripts/plot_all.py`。

> [!IMPORTANT]
> **2026-05-11 更新**：本章節因兩輪修正而大幅改寫，請以此版為準。
>
> 第一輪（方法論修正）：
> 1. 修掉 sim 內 requeue 時 reset `metrics.submit_ts` 的 bug。原本 victim JCT 只算重排後那段，低估真實等待。改後保留原始 submit_ts。
> 2. 每組 exp 從單一 deterministic sample 改成 5 個 synthetic seed（42–46），所有指標報 mean ± std 加 paired 95% CI。
> 3. Sim 加上 `--ckpt-reload-cost`（預設 60s/次）。E5 用 60s、E5b 跑 0s 當樂觀 upper bound。
>
> 第二輪（trace 廣度）：把實驗從單一 Philly-like trace 擴成三種 workload family（philly / burst / ali，共 3 × 5 = 15 個 sample），驗證結論是否 generalises。
>
> 結論：M5 predictor 在「有 contention 的 trace」上是 statistically significant 的 win（burst −28.7%、philly −20.1%、ali 因為 util 才 0.30 沒空間優化）。M7 fragmentation 在三個 trace 都是 net negative，philly +33%、burst +61%、ali +6%。舊版「E5 −28.6% vs vendor」是 submit_ts reset bug 加上單 sample 造成的人為結果。

## 1. 實驗設定

| 設定項 | 值 | 來源 |
|---|---|---|
| Trace 家族 | 三個 synthetic family，各 1000 jobs × 5 seeds | `sim.runner --trace-family {philly,burst,ali} --synth-jobs 1000 --synth-seed {42..46}` |
| └ philly | 寬鬆 Poisson arrival、log-normal runtime（median ~30 min, p95 ~6h） | `sim/loader.py::generate_philly_like` |
| └ burst | 同樣 job size mix，arrival 集中在每 6h 出現一次的 2h 高峰窗 | `generate_burst_heavy` |
| └ ali | Alibaba PAI-style：90% 單卡、median runtime ~13 min、60% 單卡 job 用 MPS 切割、晝夜節律 | `generate_ali_like` |
| Cluster | 4 nodes × 4 GPUs × 100 MPS slot | `sim.runner --nodes 4 --gpus-per-node 4` |
| Sim 模型 | discrete-event (`sim/runner.py`)，best-fit per-GPU MPS allocator，無 preempt（E5/E5b 啟用 M7 fragmentation requeue） | — |
| 重複次數 | 5 seeds × 3 traces，主結論報 mean ± std 加 paired same-seed 95% CI；E6 sensitivity 升級到 5×5 grid（25 × 3 × 5 = 375 runs） | — |
| Checkpoint reload cost | E5 = 60s/次，E5b = 0s/次 | `--ckpt-reload-cost` |

E1–E6 全部在 simulator 上跑。E7 是 live-cluster 50-job 驗證腳手架，RTX 4070 只有一張，這條路徑只能做 sim→真機 sanity check，主結論仍以 sim 為準。

| 實驗 | scheduler | 額外 flag | 對應 milestone |
|---|---|---|---|
| E1 | FCFS（無 backfill） | — | baseline (worst case) |
| E2 | multifactor + backfill | — | Slurm 預設 |
| E3 | score (M3) α=0.40, β=0.20, δ=0.20 | ε=0 | M3 完成度 |
| E4 | score + predictor (M5) | ε=0.30 | M5/M6 邊際價值 |
| E5 | score + predictor + fragmentation (M7) | `--fragmentation --ckpt-reload-cost 60` | M7 邊際價值（realistic cost） |
| E5b | E5 但 ckpt cost = 0 | `--ckpt-reload-cost 0` | M7 樂觀 upper bound |
| E6 | score + predictor，25 組 (α, δ) | 5×5 grid | sensitivity |

E5 的 fragmentation 模式 mirror 了 `operator/fragmentation.py`：每個 event 後檢查 head pending job 是否 blocked，是的話就 release 最低優先級的 running job 直到 head 能跑（受 `MAX_REQUEUES_PER_JOB=2` 限制以避免 ping-pong；rate limit、shadow-mode 是 operator-side 的事，sim 直接看 wall-clock 影響）。

## 2. 主表

每個 trace family 各 5 個 seed，下表的 jct_mean 是 5-seed 平均加標準差（單位 h），requeues 是平均每個 run 的 M7 requeue 次數。

### 2.1 philly（baseline workload）

| exp | jct_mean (h) | jct_p90 (h) | slow_mean | util | bf_rate | requeues/run | ckpt_cost (h) |
|---|---:|---:|---:|---:|---:|---:|---:|
| E1 FCFS | 15.78 ± 12.44 | 29.32 | 77.94 | 0.742 | 0.000 | 0 | 0.00 |
| E2 multifactor+bf | 4.49 ± 2.60 | 9.49 | 17.68 | 0.812 | 0.815 | 0 | 0.00 |
| E3 score (M3) | 4.71 ± 3.18 | 10.83 | 19.39 | 0.808 | 0.842 | 0 | 0.00 |
| **E4 + predictor (M5)** | **3.43 ± 1.61** | **6.35** | **10.04** | 0.805 | 0.854 | 0 | 0.00 |
| E5 + fragmentation (M7) | 4.55 ± 2.12 | 9.47 | 14.32 | 0.839 | 0.948 | 349 | 5.81 |
| E5b M7, 0s ckpt | 4.20 ± 1.70 | 9.18 | 12.34 | 0.827 | 0.950 | 347 | 0.00 |

### 2.2 burst（高峰擠壓 workload）

Job 集中在每 6 小時一次的 2 小時 burst 窗口送進來，pending queue 在 burst 期間爆衝。

| exp | jct_mean (h) | jct_p90 (h) | slow_mean | util | bf_rate | requeues/run |
|---|---:|---:|---:|---:|---:|---:|
| E1 FCFS | 17.36 ± 10.71 | 29.38 | 76.15 | 0.708 | 0.000 | 0 |
| E2 multifactor+bf | 6.36 ± 4.35 | 12.85 | 25.91 | 0.768 | 0.861 | 0 |
| E3 score (M3) | 6.62 ± 4.37 | 16.54 | 26.88 | 0.766 | 0.896 | 0 |
| **E4 + predictor (M5)** | **4.24 ± 2.10** | **8.69** | **12.76** | 0.764 | 0.906 | 0 |
| E5 + fragmentation (M7) | 6.64 ± 3.15 | 14.54 | 20.20 | 0.798 | 0.970 | 356 |
| E5b M7, 0s ckpt | 6.96 ± 4.72 | 17.14 | 21.18 | 0.784 | 0.966 | 355 |

### 2.3 ali（短 job、稀疏 workload）

ALI 系列 job 多半 ~13 min 跑完，加上 60% 單卡 job 走 MPS 分割，cluster 平均只有 ~30% util，根本沒有 contention。

| exp | jct_mean (h) | jct_p90 (h) | slow_mean | util | bf_rate | requeues/run |
|---|---:|---:|---:|---:|---:|---:|
| E1 FCFS | 0.515 ± 0.038 | 1.101 | 1.045 | 0.305 | 0.000 | 0 |
| E2 multifactor+bf | 0.512 ± 0.037 | 1.098 | 1.026 | 0.305 | 0.031 | 0 |
| E3 score (M3) | 0.512 ± 0.037 | 1.098 | 1.025 | 0.305 | 0.032 | 0 |
| E4 + predictor (M5) | 0.512 ± 0.037 | 1.099 | 1.018 | 0.305 | 0.033 | 0 |
| E5 + fragmentation (M7) | 0.543 ± 0.050 | 1.127 | 1.059 | 0.321 | 0.261 | 92 |
| E5b M7, 0s ckpt | 0.532 ± 0.047 | 1.118 | 1.037 | 0.316 | 0.231 | 75 |

### 2.4 Paired same-seed comparisons（重點）

同一個 seed 給不同 scheduler 做比較，trace 變動的雜訊被消掉，CI 比 unpaired 緊一個量級。

| trace | E4 vs E2（predictor 邊際） | E5 vs E4（M7 邊際） | E5 vs E2（整體 stack） |
|---|---:|---:|---:|
| philly | **−20.06%** ± 11.95% ↓ | **+33.08%** ± 5.22% ↑ | +6.31% ± 15.42%（不顯著） |
| burst | **−28.71%** ± 11.31% ↓ | **+60.95%** ± 31.71% ↑ | +14.85% ± 30.51%（不顯著） |
| ali | −0.08% ± 0.09%（不顯著） | **+6.03%** ± 4.48% ↑ | +5.95% ± 4.44% ↑ |

↓/↑ 代表 CI 不跨 0（statistically significant）。

數字怎麼讀：
- E1→E2：backfill 在 philly / burst 上把 mean JCT 砍 65~72%。Slurm 預設已經拿掉大部分可改善空間，後面任何 scheduler 都只在剩下的 30% 裡爭。
- E2→E3：純 M3 score 在三個 trace 上都沒有顯著改善 mean JCT。score 因子（mps_fit / vram_fit / fragmentation penalty）本身只是排序，不解決瓶頸。
- **E3→E4 (M5 predictor)**：是整個 stack 的主要 win。burst 拿到 −28.7%（CI 不跨 0）、philly −20.1%（CI 不跨 0）、ali 只有 −0.08%（util 才 0.30，連 head-of-line 都沒形成，predictor 沒空間發揮）。換句話說，predictor 的價值正比於排隊壓力。
- **E4→E5 (M7 fragmentation)**：在三個 trace 上全是 net negative。philly +33%、burst +61%、ali +6%。burst 上的損失最大，因為高峰期 head 被擋時 reconciler 觸發頻繁，但被踢的 victim 通常已經跑了一段時間，loss 大於 reschedule 帶來的好處。
- **E5b 的對照**：把 ckpt reload cost 設成 0 也救不回來。philly 從 +33% 變 +26%、burst 從 +61% 變 +63%、ali 從 +6% 變 +4%。M7 的問題不是 reload overhead，而是 victim 被踢掉後失去的 in-flight progress。

## 3. 圖

圖 1~7 用 philly trace 當代表，fig 8 是跨 trace 的 paired Δ。完整 PNG 在 `eval/figures/`，原始 CSV 在 `eval/results/<trace>/<exp>/`。

### 3.1 fig1 — JCT mean / p90 / p95（柱狀，含 95% CI error bar）

<img src="../eval/figures/fig1_jct_bars.png"/>

E1 那根壓著其他柱子一個量級，所以章節真正要看的差距在 E2..E5 之間。E4 的三根（mean / p90 / p95）都明顯比 E5 矮，error bar 也不重疊。

### 3.2 fig2 — JCT CDF（log x-axis）

<img src="../eval/figures/fig2_jct_cdf.png"/>

每條線是 P(JCT ≤ x)。E1 的 CDF 在低 x 處遠左，意思是連最快的 50% job 都比其他組慢一個量級。E4 的線整段都壓在 E5 下方，p50 / p90 / p95 全勝。

### 3.3 fig3 — Slowdown 箱型圖

<img src="../eval/figures/fig3_slowdown_box.png"/>

Slowdown = JCT / max(runtime, 60s)。E1 的 box 落在 [10, 100]，E4 收到 [1, 10] 左右，IQR 縮了一個量級。E5 的 box 比 E4 高一截，反應 requeue 後重做 lost progress 對短 job 的 slowdown 影響特別大。

### 3.4 fig4 — Utilisation timeline

<img src="../eval/figures/fig4_util_time.png"/>

把 simulated time 切 200 個 bin，每個 bin 內 RUNNING 的 (mps × gpu_count) 總和除以 cluster capacity。E5 比 E4 utilization 高一點點，但這部分高出來的工作量是「重做被 evict 的 job」，是 phantom utilization，不對應到 throughput 改善。

### 3.5 fig5 — E6 sensitivity heatmap（5×5 grid）

<img src="../eval/figures/fig5_e6_heatmap.png"/>

固定 β=0.20、ε=0.30，掃 α ∈ {0.10, 0.25, 0.40, 0.55, 0.70}、δ ∈ {0.05, 0.15, 0.20, 0.30, 0.40}，philly trace 上 jct_mean 範圍 3.28–3.63h，spread 10.6%。burst trace 上 spread 28.5%（重壓力下 weight 比較敏感），ali trace 上 spread 0.1%（沒 contention 就沒差）。三個 trace 的 best cell 都落在 α≤0.25、δ≤0.15 一帶，跟「短 job 多時把 mps_fit 拉低、fragmentation penalty 別太大」的直覺一致。

### 3.6 fig6 — backfill rate 與 M7 requeue count

<img src="../eval/figures/fig6_bf_rate.png"/>

E5/E5b 的 bf_rate 衝到 0.95，但對應的 requeue/run 是 ~349 次（philly）/ ~356 次（burst）/ ~92 次（ali）。看起來 reconciler 確實把節點塞滿了，但塞進去的有相當比例是被踢掉後重新排隊的 job，這是 fig4 phantom utilization 的根源。

### 3.7 fig7 — Mean-JCT 改善（vs E1 normalise，含 CI）

<img src="../eval/figures/fig7_jct_normalised.png"/>

E1 = 0% baseline。philly trace 上 E4 −78%、E5 −71%（E5 比 E4 退步），其他兩條 trace 同樣是 E4 最深、E5 反彈。CI error bar 在 E4 上明顯不跨 0；E5 在 burst 上 CI 比較寬反映 trace-to-trace 變動大。

### 3.8 fig8 — 跨 trace generalisation（新增）

<img src="../eval/figures/fig8_cross_trace.png"/>

三個 trace × 兩個 paired Δ。綠色是 E4 vs E2（M5 predictor 的邊際），紅色是 E5 vs E4（M7 fragmentation 的邊際）。

可以一眼讀出的事情：
- 綠色在 philly 與 burst 上明顯往下、CI 不跨 0，predictor 是真實的 win。ali 上趴在 0 附近，because 那個 trace 沒有讓 predictor 有發揮空間的 contention。
- 紅色三根都在 0 以上、CI 都不跨 0。M7 在三個 trace 上都是 net negative，不是 philly-specific artefact。

## 4. 結論與 thesis claim 對應

| Claim | 狀態 | 證據 |
|---|---|---|
| **C1** Slurm 內建 multifactor + backfill 已經把 FCFS 的 16h JCT 砍到 4~6h | ✅ paired −65~72% | E1 vs E2，三個 trace 都成立 |
| **C2** 純 M3 score（沒接 predictor）對 mean JCT 沒有顯著改善 | ✅ paired Δ 都不顯著 | E2 vs E3，三個 trace |
| **C3** M5 runtime predictor 在有 contention 的 trace 上 statistically significant | ✅ philly −20.1%、burst −28.7% | E2 vs E4 |
| **C3b** Predictor 的效益正比於排隊壓力：ali（util 0.30）上幾乎沒有差別 | 補充觀察 | E4 vs E2 on ali = −0.08% |
| **C4** M7 fragmentation reconciler 在三個 trace 上都是 net negative | ✅ generalises | E4 vs E5：philly +33%、burst +61%、ali +6%（CI 都不跨 0） |
| **C4a** 把 ckpt reload cost 設成 0 也救不回來，主因是 victim lost progress | ✅ | E5b vs E5 改善僅 6% |
| **C5** Score weight 的 sensitivity 取決於 contention：ali 0.1%、philly 10.6%、burst 28.5% | ✅，比舊版「±5%」精確 | E6 5×5 grid，三個 trace |

> [!IMPORTANT]
> C4 是 negative result，但它是這份 evaluation 想留下的核心 thesis contribution：在三個 distribution 差距很大的 synthetic trace 上，當前 M7 victim selection 都跑出 net negative。這精確定義了 reconciler 何時不該開——victim selection 純看 priority 時，會把已經跑了一大段的 job 也踢掉，loss 大於排程帶來的好處。
>
> 救援方向（給 future work）：
>
> 1. Victim selection 加上「已執行時間 / 已完成比例」penalty，避免踢已經做了一半的 job。
> 2. 改 preempt + suspend，保留 GPU memory state，把 lost progress 從 100% 降到接近 0。
> 3. 只在 predictor 估計 head 剩餘時間 < victim 剩餘時間時觸發 reconciler。
> 4. 把 M7 限縮在「ali 那類短 job heavy」的 trace 上，原本就沒幾次 requeue，傷害有限——但這也意味著 contribution 規模很小。

## 5. 風險與限制

- **三個 trace 都是 synthetic**。philly / burst / ali 是三組不同 distribution 的 generator，已經把 negative result 跨 distribution 確認過，但真實 production trace（如 Helios、Alibaba 公開 trace）仍可能呈現不同的 contention 結構。如果未來有真實 trace 可用，repro 就只是換 `--trace` 參數的事。
- **Lost-progress 保守假設**。Sim 讓 victim 從零重跑，沒模擬 partial checkpoint resume。E5 已經加 60s reload overhead，但「已完成的 work 100% 重做」這個假設偏悲觀。實際 preempt+suspend（保留 GPU memory）可以把這部分大幅縮小，這也是 C4 future work 的方向 2。
- **Predictor 假設完美**。Sim 裡 `f_runtime_short(true_runtime)` 等於假設 RMSE = 0。`services/runtime_predictor` 實測 RMSE 約 ±20%，真實環境 E4 改善幅度大概要打 8 折。
- **mem / IO 沒模擬**。Sim 只追蹤 MPS slot 跟 GPU 數，GPU job 的 IO wait 也是 JCT 顯著貢獻者，這部分被當成 "已內含於 runtime" 處理。
- **E6 grid 已升級到 5×5**，但仍沒掃 β 跟 ε。如果要進一步聲明 weight robustness 需要 full 4-D scan。
- **Live cluster 尚未驗證（handoff #1）**。E7 live harness 已寫好但只是 feasibility 規模（單張 RTX 4070），主結論仍 100% 來自 simulator。

## 6. 重現步驟

```bash
# 0. （第一次跑）裝 venv
uv venv .venv-m5 && uv pip install --python .venv-m5/bin/python pytest matplotlib

# 1. 跑 E1..E6（< 5 分鐘）
bash eval/scripts/run_all.sh

# 2. 出圖（< 10 秒）
.venv-m5/bin/python eval/scripts/plot_all.py

# 3. 看主表
.venv-m5/bin/python eval/scripts/print_summary.py

# 4. （optional）E7 live cluster — 需要 kubeconfig 指到一個跑 chart 的 k3s
bash eval/scripts/run_e7_live.sh our      # M3+M5+M7 stack
helm upgrade ... -f vendor-baseline.yaml  # 翻成 multifactor-only
bash eval/scripts/run_e7_live.sh vendor
```

raw artifacts：
- `eval/results/<trace>/<exp>/<run>__seed<N>.csv` 每個 seed 的 per-job
- `eval/results/<trace>/<exp>/<run>__seed<N>.json` per-run summary
- `eval/results/all_summaries.json` 全部 465 runs 攤平（3 traces × 5 seeds × 31 configs）
- `eval/results/agg_by_run.json` 跨 seed 聚合的 mean / std / 95% CI
- `eval/figures/fig{1..8}.{png,pdf}` 圖

可以調整的環境變數：
- `TRACES="philly burst ali"` 控制要跑哪幾種 trace
- `SEEDS="42 43 44 45 46"` 控制 seed 列表
- `E6_GRID=DENSE` (5×5) 或 `SMALL` (3×3 legacy)
- `CKPT_COST=60.0` E5 的 checkpoint reload cost

## 7. M8 驗收

- [x] **跨 trace raw data 齊全** — 3 traces × 5 seeds × 31 configs = 465 sim runs；E7 harness 仍待 live cluster 跑
- [x] **8 張圖出爐** — fig1 bar、fig2 CDF、fig3 box、fig4 utilization、fig5 heatmap、fig6 bf+requeue、fig7 normalised、fig8 cross-trace
- [x] **negative result 跨 distribution 驗證** — M7 在 philly / burst / ali 三種完全不同 distribution 上一致 net negative，不是單 trace artefact
- [x] **eval-writeup.md** — 本檔 §1–§7；每張圖都有 1–2 段論述
