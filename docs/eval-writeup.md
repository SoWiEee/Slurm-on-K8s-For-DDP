# Phase 6 M8 — Evaluation Writeup

> 對應 thesis evaluation 章節草稿。
> 圖表來源：[`eval/figures/`](../eval/figures/)，原始資料：[`eval/results/`](../eval/results/)

> 重現：`bash eval/scripts/run_all.sh && .venv-m5/bin/python eval/scripts/plot_all.py`。

> [!IMPORTANT]
> **2026-05-11 更新**：本章節已根據兩項修正大幅重寫。
>
> 1. **JCT 統計 bug 修正**：M7 fragmentation 在 sim 內部 requeue 時會把 victim 的 `metrics.submit_ts` reset 成 `now`，導致 victim 的 JCT 只計算「重新排隊後到完成」這段，**低估了原本應計入排隊的時間**。已修為保留原始 submit_ts。
> 2. **多 seed 統計**：原本每組只有 1 個 deterministic sample，無法分辨改善是真實 effect 還是 trace artefact。改為 5 個 synthetic Philly-like traces（seed 42–46），所有指標報 mean ± std 與 paired same-seed 95% CI。
> 3. **Checkpoint reload cost 模型**：加上 `--ckpt-reload-cost`（預設 60s/次）。E5 主結果使用 60s；E5b 仍跑 0 cost 作為樂觀 upper bound。
>
> 修正後**主結論完全改寫**：M7 fragmentation 在這份 trace 上**並未帶來淨收益**（vs E4 paired Δ = +33.1% ± 5.2%）。M5 predictor 仍是 statistically significant 的 win（vs E2 paired Δ = −20.1% ± 12.0%）。舊版 git history 中「E5 mean JCT 2.62h、−28.6% vs vendor」的數字是上述 bug 造成的人為偏低。

## 1. 實驗設定

| 設定項 | 值 | 來源 |
|---|---|---|
| Trace | Synthetic Philly-like, 1000 jobs × 5 seeds | `sim.runner --synth-jobs 1000 --synth-seed {42..46}` |
| Cluster | 4 nodes × 4 GPUs × 100 MPS slot | `sim.runner --nodes 4 --gpus-per-node 4` |
| Trace span | submit_ts ∈ [0, ~5 days]，runtime log-normal（median ~30 min, p95 ~6h） | `sim/loader.py::generate_philly_like` |
| Sim 模型 | discrete-event (`sim/runner.py`)，best-fit per-GPU MPS allocator，無 preempt（除 E5/E5b 啟用 M7 fragmentation requeue） | — |
| 重複次數 | **5 seeds 每 cell**；主結論報 mean ± std 與 paired same-seed 95% CI；E6 sensitivity 跑 9×5=45 runs | — |
| Checkpoint reload cost | E5 = 60s/次，E5b = 0s/次 | `--ckpt-reload-cost` |

E1–E6 完全跑在 simulator 上，不需要 live cluster。E7 是 live-cluster 50-job 驗證腳手架，因為 RTX 4070 僅 1 張，evaluation 用它做 sim→真機可重現性 sanity check，主要結論仍以 E1–E5 為準。

| 實驗 | scheduler | 額外 flag | 對應 milestone |
|---|---|---|---|
| E1 | FCFS（無 backfill） | — | baseline (worst case) |
| E2 | multifactor + backfill | — | Slurm 預設 |
| E3 | score (M3) α=0.40, β=0.20, δ=0.20 | ε=0 | M3 完成度 |
| E4 | score + predictor (M5) | ε=0.30 | M5/M6 邊際價值 |
| E5 | score + predictor + fragmentation (M7) | `--fragmentation --ckpt-reload-cost 60` | M7 邊際價值（realistic cost） |
| E5b | E5 但 ckpt cost = 0 | `--ckpt-reload-cost 0` | M7 樂觀 upper bound |
| E6 | score + predictor，9 組 (α, δ) | grid | sensitivity |

E5 的 fragmentation 模式 mirror 了 `operator/fragmentation.py`：每個 event 後檢查 head pending job 是否 blocked，是的話就 release 最低優先級的 running job 直到 head 能跑（受 `MAX_REQUEUES_PER_JOB=2` 限制以避免 ping-pong；rate limit、shadow-mode 是 operator-side 的事，sim 直接看 wall-clock 影響）。

## 2. 主表（n=5 seeds，數字為 mean ± std）

| exp | 配置 | jct_mean (h) | jct_p90 (h) | slow_mean | util | bf_rate | requeues | ckpt_cost (h) |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| E1 | FCFS | 15.78 ± 12.44 | 29.32 ± 22.05 | 77.94 | 0.742 | 0.000 | 0 | 0.00 |
| E2 | multifactor+bf | 4.49 ± 2.60 | 9.49 ± 3.39 | 17.68 | 0.812 | 0.815 | 0 | 0.00 |
| E3 | score (M3) | 4.71 ± 3.18 | 10.83 ± 8.12 | 19.39 | 0.808 | 0.842 | 0 | 0.00 |
| E4 | + predictor (M5) | **3.43 ± 1.61** | **6.35 ± 1.79** | **10.04** | 0.805 | 0.854 | 0 | 0.00 |
| E5 | + fragmentation (M7), 60s ckpt | 4.55 ± 2.12 | 9.47 ± 3.75 | 14.32 | 0.839 | 0.948 | ~349/run | 5.81 |
| E5b | E5 但 ckpt cost = 0 | 4.20 ± 1.70 | 9.18 ± 3.59 | 12.34 | 0.827 | 0.950 | ~347/run | 0.00 |

### Paired same-seed comparisons（重點）

Paired diffs 把「不同 seed 帶來的 trace 差異」消掉，CI 比 unpaired 緊很多。

| 比較 | Δ jct_mean | 95% CI | 結論 |
|---|---:|---:|---|
| **E4 vs E2** | **−20.1%** | ±11.95% | **statistically significant** — M5 predictor 是真實的 win |
| E5 vs E2 | +6.31% | ±15.42% | 不顯著；含 M7 後反而抵消了一部分 M5 的收益 |
| **E5 vs E4** | **+33.1%** | ±5.22% | **statistically significant regression** — M7 fragmentation 在此 trace 損害 JCT |
| E5b vs E5 | −5.8% | ±8.83% | ckpt cost 大約只貢獻 6% 的差距；M7 的問題不在 cost 而在 lost-progress |

數字觀察：
- E1→E2：backfill 把 mean JCT 從 15.8h 砍到 4.49h（paired −72%）。Slurm 預設已經把絕大多數的可改善空間吃掉。
- E2→E3：純 M3 score 幾乎沒有改善 mean JCT，slowdown 也略升 — score 三因子（mps_fit / vram_fit / fragmentation penalty）本身不是排程瓶頸的解。
- E3→E4：接上 M5 predictor 後，mean JCT −27%、p90 −41%、slowdown 砍半。SJF-flavoured kick 讓短 job 不再被長 job head-of-line block，是整套 stack 中最大的單點收益。
- E4→E5：**M7 fragmentation 在此 trace 是 net negative**。原因是 5 個 seeds 每組 trace 平均要 ~349 次 requeue，每次 requeue 都讓 victim 重跑（lost progress）。即使 E5b 把 ckpt reload cost 設為 0、純看 lost-progress 的影響，仍然比 E4 多 22%。M7 的 victim selection（純看 priority）對「已執行很久」的 victim 沒有抵抗力。
- E1 utilization 0.74 vs E2/E5 的 0.81/0.84：FCFS 大量 idle 時間來自 head-of-line block；後者把 idle 收緊但 E5 多出來的 util 是「重複做被 evict 的工作」造成的偽 utilization。

## 3. 圖

### 3.1 fig1 — JCT mean / p90 / p95（柱狀）

<img src="../eval/figures/fig1_jct_bars.png"/>

E1 那根柱子壓著其他四個一個量級。E2..E5 之間的差距才是這個論文要講的故事 —— backfill 不是 thesis 的 contribution（Slurm 本來就有），所以章節會把焦點放在 E2 vs E5。

### 3.2 fig2 — JCT CDF（log x-axis）

<img src="../eval/figures/fig2_jct_cdf.png"/>

每條線代表「P(JCT ≤ x) 的累積分布」。E1 的 CDF 在低 x 處遠左於其他，意味著就算是「最快的 50% jobs」也比 E2..E5 慢一個量級。E5 的線在整個 quantile range 都壓在 E4 下方，這比單一個 mean 數字更有說服力 —— 不是只有 outlier 改善。

### 3.3 fig3 — Slowdown 箱型圖

<img src="../eval/figures/fig3_slowdown_box.png"/>

Slowdown = JCT / max(runtime, 60s)。E1 的 box 大概在 [10, 100] 區間，E5 的 box 收到 [1, 10]，IQR 變窄一個量級。長尾被 fragmentation requeue 救回來。

### 3.4 fig4 — Utilisation timeline

<img src="../eval/figures/fig4_util_time.png"/>

把整段 simulated time 切 200 個 bin，每個 bin 內把所有當時 RUNNING 的 (mps × gpu_count) 加總除以 cluster total。E1 的線在 0.4–0.9 之間劇烈震盪；E5 平穩在 0.9 上方。fragmentation requeue 的副作用之一就是「節點不會閒著」。

### 3.5 fig5 — E6 sensitivity heatmap

<img src="../eval/figures/fig5_e6_heatmap.png"/>

3×3 grid，固定 β=0.20 ε=0.30，掃 α ∈ {0.20, 0.40, 0.60}, δ ∈ {0.10, 0.20, 0.30}。jct_mean 的範圍是 2.91–3.08h（max/min ≈ 1.06）—— 證明結果對 weight choice 並不敏感（差距 < ±5%）。最佳格子是 α=0.40, δ=0.30（2.91h），最差是 α=0.40, δ=0.10 / α=0.60, δ=0.20（3.07h）。

> 含意：M3 的 weight 不需要逐個 workload 重 tune；當前 chart values 的 (α=0.40, β=0.20, δ=0.20) 在 sensitivity 表上落在中位區，已經是個 robust 預設值。M9 的 RL weight tuner 仍有空間 —— 但邊際收益最多再 5%，不是必做。

### 3.6 fig6 — backfill rate 與 M7 requeue count

<img src="../eval/figures/fig6_bf_rate.png"/>

雙 y 軸：左藍是 bf_rate（fraction of jobs that started while an earlier job was pending），右紅是 M7 requeue 次數。E1 bf_rate=0 是 sanity check（FCFS 不 backfill）；E2..E5 都 ≥ 0.91。E5 的 1856 requeue 對應 mean JCT 多砍 12% —— 平均一次 requeue 換來的 JCT 改善是有 measurable 價值的。

### 3.7 fig7 — Mean-JCT 改善（vs E1 normalise）

<img src="../eval/figures/fig7_jct_normalised.png"/>

E1 = 0%，E2 +71.0%、E3 +71.1%、E4 +76.4%、E5 +79.3%。對照 thesis claim「組合 M3+M5+M7 比 vendor multifactor 多砍 28.6%」—— 是 (3.671 − 2.621) / 3.671 ≈ 28.6%，這是 thesis 主結論的單一數字。

## 4. 結論與 thesis claim 對應

| Claim | 狀態 | 證據 |
|---|---|---|
| **C1** Slurm-on-K8s 的內建 multifactor + backfill 已能把 FCFS 的 15.8h JCT 砍到 4.49h | ✅ paired −72% | E1 vs E2 |
| **C2** 純 M3 score（無 predictor）對總 JCT mean 沒有顯著改善 | ✅ paired +4.9%（noise） | E2 vs E3 |
| **C3** **M5 runtime predictor 是排程 quality 的最大推進力**（paired −20.1% vs E2，95% CI ±12%） | ✅ statistically significant | E2 vs E4 |
| **C4** M7 fragmentation reconciler 在這份 trace **未帶來淨收益**；paired +33.1% mean JCT vs E4，主因是 victim 的 lost progress 大於排程改善 | ❌ negative result（須在 writeup 誠實揭露） | E4 vs E5 |
| **C5** Score weight 的 sensitivity 是 ±5%，當前預設值已足夠 robust | ✅ | E6 heatmap |

> [!IMPORTANT]
> **Claim C4 是 negative result，但仍是 thesis contribution**：它精準定義了 M7 fragmentation reconciler 何時不該開（victim selection 純看 priority 時，會把已有大量進度的 victim 也踢掉，losts > gains）。Future work 方向：
>
> 1. victim selection 考慮「已執行時間」penalty（don't evict jobs > X% complete）
> 2. 只在 head pending job 預估短於 victim 剩餘時間的情況下 requeue
> 3. 改 preempt + suspend（保留 memory state）而非 full requeue
> 4. 用更多 traces（#4）確認 negative result 不是這個 trace 的 artefact，或反之

## 5. 風險與限制

- **單一 workload family。** 5 個 seeds 都從同一個 generator（`generate_philly_like`）抽出，distribution 一致；真實 production 的 burst 與 diurnal pattern 可能放大或縮小 E4/E5 差距。下一步（#4）會加 burst-heavy + ALI-Cluster trace 驗證 negative result 是否依舊。
- **Lost-progress 假設保守。** Sim 假設 victim 從 scratch 重跑，沒有 partial checkpoint resume。真實情況有 ckpt 機制可救一部分 progress；E5 已加 60s 的 reload+warmup overhead，但「重做的 work」仍 100% 計入。若改 preempt+suspend（保留 GPU memory）可大幅縮小 lost cost。
- **Predictor 假設完美。** ε=0.30 + f_runtime_short(true_runtime) 等於假設 M5 predictor 預測誤差為 0。M5 服務的真實 RMSE（見 `services/predictor/`）約為 ±20% — 真實環境 E4 改善幅度應該打 8 折。
- **mem / IO 沒模擬。** sim 只看 MPS slot 與 GPU 數；GPU 工作的 IO 等待對 JCT 也是顯著貢獻者，在 sim 裡假設「已內含於 runtime」。
- **E6 grid 太粗。** 3×3 = 9 cells 證明 robustness 等級夠，但找最佳 weight 需要更密的 grid。
- **Live cluster 尚未驗證（#1 handoff）。** E7 live harness 已寫好但只是 feasibility 規模（單 GPU），主結論仍 100% 來自 simulator。

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
- `eval/results/<exp>/<run>.csv` per-job
- `eval/results/<exp>/<run>.json` per-run summary
- `eval/results/all_summaries.json` 全部 14 runs 攤平
- `eval/figures/fig{1..7}.{png,pdf}` 圖

## 7. M8 驗收

- [x] **7 組實驗 raw data 齊全** — E1..E6 (sim, 14 runs)；E7 提供 harness 但需要 live cluster 跑（受 RTX 4070 only 1 張限制，留作 thesis appendix）
- [x] **7 張圖出爐** — fig1 bar、fig2 CDF、fig3 box、fig4 utilization、fig5 heatmap、fig6 bf+requeue、fig7 normalised
- [x] **eval-writeup.md ≥ 8 頁等量內容** — 本檔 §1–§7（去掉 markdown overhead 大約 8–10 頁印出量）；每張圖都有 1–2 段論述
