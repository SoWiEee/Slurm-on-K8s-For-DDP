# 排程 score function 規格（最終版）

> **狀態（2026-05-13）**：M3 + M5 + M6 + M9（含 live 部署）完成；M7 topology 因子仍未實作（γ=0）。本規格反映目前實際跑在 `chart/templates/configmap-job-submit.yaml` 與 `sim/scheduler/score.py` 裡的版本。
>
> **Milestone 進展**：
> - M2（2026-05-06）：lua scaffold，每個因子都回 0.5，係數先用手調的。
> - M3（2026-05-06）：`f_mps_fit`、`f_vram_fit`、`f_fragmentation` 上線。
> - M5/M6（2026-05-07）：runtime-predictor service 跟 lua 接通；`f_runtime_short` 啟用；ε 從 0.05 調到 0.30。
> - M7（2026-05-10）：fragmentation reconciler 走 operator 那條路（不是新增 score 因子）；預設 shadow mode。
> - M8（2026-05-11）：跨 trace sensitivity sweep 加 paired CI；γ 維持 0（topology 沒做）。
> - M9 sim（2026-05-12）：UCB1 offline weight tuner，學出來的 arm 跟 grid-best 差 3% 以內。
> - M9 live（2026-05-13）：`weight-tuner` service 上線（k3s）；`f_pred_runtime` 接通 predictor；UCB1 arm 動態覆蓋 (α,δ,ε)；score 從 0.200→0.497（見 §8）。

## 1. 用途

`score(J, P)` 是一個純量，submit 時用來排 (job, partition) 候選的優先順序。分數越高越值得先排。Slurm 內建 priority 已經把 fairshare、age、partition 那些做完了；我們的 score 疊加上去，讓「純 Slurm」跟「Slurm + score」可以 A/B 比較，不用重寫底層。

## 2. 公式

```
score(J, P) = α · f_mps_fit(J, P)         ∈ [0, 1]
            + β · f_vram_fit(J, P)        ∈ [0, 1]
            + γ · f_topology(J, P)        ∈ [0, 1]   (γ = 0; 未實作)
            - δ · f_fragmentation(J, P)   ∈ [0, 1]
            + ε · f_runtime_short(J)      ∈ [0, 1]
```

每個 `f_*` 都回傳 `[0, 1]` 區間的值。整條算式 clamp 到 `[0, 1]` 後套用：

```
job_desc.priority = base_priority + round(SCORE_GAIN · score)
```

`base_priority` 是 Slurm 內建 multifactor 的輸出；`SCORE_GAIN` 是 chart 設定（預設 `1000`，約等於 `PriorityWeightAge / hour`，所以滿分 score 大概買到 1 小時的 age）。

`job_submit.lua` 裡這個因子的函式名是 `f_pred_runtime`（保留 M2 時取的名）；`sim` 用 `f_runtime_short`。同一個東西。

### 係數（M5 後的 production 預設值）

| Factor             | Symbol | Default | M8 sensitivity spread       | M9 UCB1 pick |
|--------------------|:------:|:-------:|:---------------------------:|:------------:|
| MPS fit            |   α    |  0.40   | 0.10–0.70（jct_mean ±10.6%）|     0.10     |
| VRAM fit           |   β    |  0.20   | M8/M9 維持固定               |     0.20     |
| Topology           |   γ    | **0**   | 未實作                      |     0        |
| Fragmentation cost |   δ    |  0.20   | 0.05–0.40（jct_mean ±10.6%）|     0.05     |
| Predicted runtime  |   ε    |  0.30   | 0.00–0.60                   |     0.60     |

係數寫在 `chart/values.yaml` 的 `slurm.jobSubmit.scoreWeights`。Production 改動要配一筆 §6 sensitivity 紀錄。

**M8/M9 跑完發現兩件當初 M2 沒想到的事**：

1. Sensitivity 是 workload-dependent。M8 跑 (α, δ) 5×5 grid 配三個 trace family：ali 0.1%、philly 10.6%、burst 28.5% mean-JCT spread。低 contention 的 workload 幾乎不會從 weight tuning 拿到東西，高 contention 才有 25% 以上的空間。M9 UCB1 也反映這個——它挑出來的 arm 偏向 burst 上最佳，因為那邊 reward signal 最強。
2. M9 挑出的 arm（α=0.10, ε=0.60）偏向「讓 predictor 多做事」。預設 (α=0.40, ε=0.30) 是 M5 還沒接時手調的，那時 predictor 等於 0，所以 mps_fit 必須擔大。M5 接上後 predictor 變成可用訊號，把 mps_fit 的權重讓出來給它比較合理。

## 3. Factor 規格

### 3.1 `f_mps_fit(J, P)` — MPS slot 配適

衡量 job 的 MPS 需求塞到單一 node 剩餘 slot 的合身程度。

| 欄位          | 定義 |
|--------------|------|
| **Input**    | `job_desc.tres_per_node`（例如 `"gpu:rtx4070:1,mps:25"`）；`sinfo -O Gres` 拿到每 node 的 `mps_free` |
| **Output**   | 在最佳候選 node 上剛好塞滿 → `1.0`，殘留越多分數越低 |
| **Formula**  | `1 - residual_mps / 100`，其中 `residual_mps = best_node.mps_free - job.mps_req` |
| **Edge case**| `job.mps_req == 0`（沒申請 MPS）→ `1.0`，不扣分 |
| **Edge case**| 沒有 node 的 `mps_free ≥ mps_req` → `0.0` |
| **Owner**    | `job_submit.lua::f_mps_fit`（M3） |

### 3.2 `f_vram_fit(J, P)` — VRAM tier 配適

避免小 VRAM 的 job 被丟到大 VRAM node 浪費，反之亦然。

| 欄位          | 定義 |
|--------------|------|
| **Input**    | `job_desc.features` 的 vram 限制（例如 `vram-12g+`）；node 上的 Feature label（`vram-12g`、`vram-24g`）|
| **Output**   | 最小可用 tier 剛好 → `1.0`；大一檔 → `0.5`；沒 tier 符合 → `0.0` |
| **Formula**  | `1 - (vram_node - vram_min) / max_vram`，clamp 到 `[0, 1]` |
| **Edge case**| 沒給 `vram-*` 限制 → `0.5`（中性）|
| **Edge case**| Node 沒有任何 `vram-*` Feature → `0.5`（中性，不要懲罰 CPU job） |
| **Owner**    | `job_submit.lua::f_vram_fit`（M3） |

### 3.3 `f_topology(J, P)` — NCCL collective 親和性 *（未實作，γ=0）*

讓 multi-node DDP job 偏好走同一條快速 interconnect。

| 欄位          | 定義 |
|--------------|------|
| **Input**    | `job_desc.min_nodes`；node Feature label（`net2-10g`、`net2-25g`）；`worker-pools.json` 的 `topology` group |
| **Output**   | 候選 nodes 全在同一 topology group → `1.0`；分散 → `0.0` |
| **Formula**  | `count(same_group) / job.min_nodes` |
| **Edge case**| `min_nodes == 1` → `1.0`（單 node job 沒 collective）|
| **狀態**     | **未實作**。lua 回 0.5 stub、γ 在 production 設 0 所以不影響結果。當前 gpu-rtx4070 / gpu-rtx4080 兩個 pool 本來就單 node，沒地方測 multi-node DDP affinity。要做要先補 Multus、per-pool topology label、以及一份 multi-node DDP trace。 |

### 3.4 `f_fragmentation(J, P)` — 排上去後的碎片化代價

跟 Gandiva 想法一樣：node 之間 free MPS 越平均越健康，集中在一個 node 反而不好。

| 欄位          | 定義 |
|--------------|------|
| **Input**    | 假設這個 job 排上去後，每 node 的 `mps_free` |
| **Output**   | `0.0` 沒碎片化、`1.0` 最碎 |
| **Formula**  | `stddev(mps_free) / 100`（每 node 100 個 MPS slot） |
| **Edge case**| 單 node → `0.0`（stddev 未定義）|
| **Owner**    | `job_submit.lua::f_fragmentation`（M3） |

### 3.5 `f_runtime_short(J)` — M5 predictor 餵的 SJF kicker

短 job 給高分，用 SJF 的直覺把 mean JCT 拉低。預測 runtime 來自 M5 LightGBM service。Lua 函式名是 `f_pred_runtime`（M2 留下來的），sim 那邊叫 `f_runtime_short`。

| 欄位          | 定義 |
|--------------|------|
| **Input**    | `POST /predict` 拿到的 `pred_seconds`。Lua 從 `job_desc.user_name`、`tres_per_node`、`min_nodes`、`features`、`time_limit` 抽 feature 送過去。 |
| **Formula**  | `f_runtime_short = horizon / (horizon + pred_seconds)`，預設 `horizon=3600` 秒（1 小時）。 |
| **Output**   | 瞬時 job → `→ 1.0`；`pred=horizon` → 0.5；遠超 horizon → `→ 0`。 |
| **飽和特性** | `f` 對 `pred_seconds` 是 sub-linear，所以單一個極長預測值不會把整段壓死；spread 取決於 `pred_seconds / horizon` 的範圍。 |
| **Edge case**| Predictor timeout（curl --max-time 200ms）→ 回 `(false, nil, "timeout")`；lua 不動 `priority`；`f` 算 0.5（中性）。 |
| **Edge case**| Predictor 關掉（`slurm.jobSubmit.predictor.enabled=false`）→ `f=0.5`（中性）。 |
| **Edge case**| Predictor 回 bootstrap 模式（`model_version="bootstrap"`、n_train < MIN_TRAIN_SAMPLES）→ 仍照常吃預測值（值是 `min(user_time_limit, 4h)`，合理），lua 從 response 的 `bootstrap=true` flag 知道，但不切到 neutral。 |
| **Owner**    | `services/runtime_predictor/` 加 `chart/templates/configmap-job-submit.yaml::call_predictor()` |

**`horizon` 該怎麼設**：M8/E7 觀察告訴我們，`horizon` 應該跟 workload 的 p50–p90 runtime 同量級。E7 上 workload 只跨到 360 秒、`horizon=3600` 讓 `f` 的 spread 從 0.937 到 0.976（差 0.04），predictor 的訊號被 ε 縮成 priority 上的 12 點，換算到等待時間只值 43 秒，**完全淹沒在 noise 裡**。Production 想看到 sim 等級的改善，把 `horizon` 拉到接近 workload p50–p90，或者直接加大 ε。

**`applyTimeLimit` 預設應該關掉**：lua 的 `call_predictor` 預設 `applyTimeLimit=true`，會把 `job_desc.time_limit` 改寫成預測值。E7 v2 撞到的問題是模型對某 user 預測 150 秒、但實際 job 跑 250 秒，Slurm 在 150 秒砍掉變 TIMEOUT。**除非你有 calibrated predictor 加明確的 walltime budget 需求，否則 chart 上 `slurm.jobSubmit.predictor.applyTimeLimit` 設 `false`，predictor 只做排序、別動 walltime**。

## 4. I/O schema

### 4.1 `job_submit.lua` 進入點

```lua
function slurm_job_submit(job_desc, part_list, submit_uid)
  -- 唯讀：job_desc.{name, partition, num_tasks, min_nodes,
  --              tres_per_node, features, time_limit,
  --              user_id, account, qos, ...}
  -- 可寫：job_desc.{priority, time_limit, comment}
  -- 回傳：slurm.SUCCESS 或 slurm.ERROR
end
```

### 4.2 Predictor HTTP contract（M5+M6，當前 production）

Lua 用 `string.format` 直接拼 JSON（controller image 沒裝 cjson）；predictor 的 Pydantic schema 在 `services/runtime_predictor/app.py::PredictRequest`。

```
POST /predict
Content-Type: application/json
  {
    "user": "alice",
    "partition": "gpu-rtx4070",
    "gpu_count": 1,
    "mps_req": 25,
    "gpu_type": "rtx4070",
    "user_time_limit_seconds": 3600
  }

  → 200 OK
  {
    "pred_seconds": 1146.28,
    "pred_minutes": 19.10,
    "model_version": "lgbm-v1",
    "bootstrap": false,
    "latency_ms": 1.12
  }
```

失敗模式（lua 一律當成「沒拿到預測」，`f=0.5`）：

| 條件 | lua 看到的 | 行為 |
|---|---|---|
| `curl --max-time 200ms` timeout | shell exit 28 | `priority` 不動 |
| HTTP 5xx（service down） | curl exit 0 但 body 空 / 非 JSON | `parse_predict_response` 回 nil，不 override |
| Body 沒有 `pred_seconds` | 同上，parse 回 nil | 不 override |
| Chart 把 `slurm.jobSubmit.predictor.enabled` 設 false | lua 整個 `call_predictor` block 跳過 | `f=0.5` stub |
| Predictor 在 bootstrap 模式（n_train < 100） | response 帶 `bootstrap=true` | 仍照值使用（`min(user_time_limit, 4h)`，安全） |

200ms timeout 是刻意給很緊的。slurmctld 在 lua plugin 上是 single-thread，hung predictor 會拖慢每一個 sbatch 最多 200ms。實測 predictor latency p99 < 5ms（e7 cluster 上 108 次預測，p99 < 2ms）。

## 5. 測試 case（busted，M3+）

```
describe "f_mps_fit" {
  it "exact fit 回 1.0" {
    -- job mps=25，唯一 node 的 mps_free=25 → 1.0
  }
  it "沒申請 mps 回 1.0" {
    -- 沒要求 MPS 的 job 不該被扣分
  }
  it "沒 node 塞得下回 0.0" {
    -- mps_req 大於所有 node 的 mps_free
  }
  it "對 residual 單調遞減" {
    -- residual=10 < residual=50 ⇒ score(10) > score(50)
  }
}

describe "f_fragmentation" {
  it "單 node 回 0.0" { ... }
  it "free MPS 平均分散時接近 0.0" { ... }
  it "free MPS 集中時 ≥ 0.3" { ... }
}

describe "score（複合）" {
  it "預設 weight 加總 ≤ 1" {
    -- α+β+γ+ε ≤ 1，δ 是減項所以 total ∈ [-δ, α+β+γ+ε]
  }
  it "fragmentation penalty 真的會扣分" { ... }
}
```

## 6. Sensitivity 紀錄

| 日期       | 變更                                | Workload         | JCT delta | 備註 |
|------------|---------------------------------------|------------------|-----------|-------|
| 2026-05-06 | M2 scaffold（沒因子啟動）             | verify.sh        |     0%    | Baseline；lua plugin loaded 但每個因子都回 0.5 |
| 2026-05-06 | M3（α=0.40, β=0.20, δ=0.20, γ=ε=0）    | sbatch 5-mix     |    n/a    | 27/27 lua unit + 79/79 helm-unittest 綠。Live priority 跟手算對得起來。Whole-node mps=100 主導；中段 mps=50 被 δ 扣最多。 |
| 2026-05-07 | M4 simulator baseline（α=0.40, β=0.20, δ=0.20） | Philly-like 1k @ 4×4 | jct_mean −0.7 % vs multifactor；**wait_p90 −20.8 %**；bf_rate +3.2 pp | Score kicker 把小 well-fit job 排到大 head-of-line job 前面。 |
| 2026-05-07 | M6 plumbing（predictor 啟用、applyTimeLimit=true）| 離線 E2E（lua + curl + uvicorn）| n/a | lua → predictor round-trip 跑通；live mode 把 `time_limit=0` 改成 10 min，fallback mode 保持 0。 |
| 2026-05-11 | **M8 跨 trace**（α=0.40, β=0.20, δ=0.20, ε=0.30） | 3 traces × 5 seeds（Philly/burst/ali） | paired Δ vs vendor multifactor：**philly −20.1%、burst −28.7%、ali −0.08%** | 兩個 contention-heavy trace 統計顯著；ali（util 0.30）沒空間給 predictor 發揮。M5 是整套 stack 的主要 win。詳見 `docs/eval-writeup.md` §4。 |
| 2026-05-11 | **E6 5×5 sensitivity grid**（α ∈ {0.10..0.70}, δ ∈ {0.05..0.40}, ε=0.30）| 同 3×5 | jct_mean spread：**ali 0.1%、philly 10.6%、burst 28.5%** | Weight sensitivity 跟 contention 正相關；best cells 落在 α ≤ 0.25、δ ≤ 0.15。Workload-dependent，低 contention cluster 不需要 tune。 |
| 2026-05-11 | M7 fragmentation reconciler（operator，**不是** score 因子）| 同 3×5 | paired Δ vs E4：**philly +33.1%、burst +60.9%、ali +6.0%** | Net negative 跨三個 distribution 一致。Victim-selection-by-priority 會踢掉跑了一半的 job，lost progress 大過排程改善。E5b 對照組（ckpt cost=0）證實主因是 lost progress 不是 reload overhead。M7 留 `shadowMode=true`，不進 production。 |
| 2026-05-11 | E7 live `our`（M3 score only）| k3s + 1 × RTX 4070, 20-job mix | paired Δ vs vendor：**−38.6%**（乾淨子集）| Headline −57.7% 有一半是 vendor pass 撞到 AllocTRES freeze 撐高的；只看 cluster 暖機後的 small job，paired diff 是 −38.6%，這個可以放進論文 main result。 |
| 2026-05-11 | E7 live `our_pred`（M3 + M5，bootstrap predictor on Philly synthetic）| 同 | vs `our`：−0.13%（noise） | Bootstrap 模型對 e7 workload 預測 766s、實際 60–120s，signal 指錯方向。 |
| 2026-05-12 | E7 live `our_pred_hetero_v2`（matched predictor，MAE_log 0.34）| 同 | vs `our` (homogeneous)：−0.08%（仍 noise） | 模型對齊也救不回來，workload runtime spread 只 6×，`f_runtime_short` 的 spread 只 0.04，換算到 priority 只值 43 秒等待。這指出 production 部署的第三個必要條件：horizon / ε 要跟 workload 跨度配。 |
| 2026-05-12 | **M9 UCB1 weight tuner**（services/weight_tuner, offline） | 3 traces × 5 seeds, 27 arms, 120 rounds | eval JCT 2.587h vs M8 grid-best 2.511h（**+3.0%**，vs oracle 2.448h +5.7%） | Picked arm（α=0.10, δ=0.05, ε=0.60），predictor-heavy。比 M8 grid-best 省 1/3 sim 預算（120 vs 375）。LinUCB 不如 UCB1，因為 oracle vs static-best 只差 2.5%，context 沒空間發揮。PPO 不做（同樣的上限）。|

（後續實驗附在這。）

## 7. Boundary policy

- **Plugin 失敗**：`slurm_job_submit` 拋 Lua error 時，slurmctld fallback 到 `slurm.SUCCESS`（priority 不動）。`slurmctld.log` level `error` 留記錄。
- **Predictor 不可用**：`f_runtime_short` 回 0.5（見 §3.5）。
- **Score < 0**（δ 主導）：clamp 到 0；`job_desc.priority` 不動。Level `info` 留記錄。
- **Score > 1**：clamp 到 1。
- **`SCORE_GAIN = 0`**（chart 設定）：plugin 照跑照 log，但不動 priority。M8 shadow-mode 評估用。

## 8. M9 — Bandit weight tuning

M2 spec 假設係數會永遠手調。M8 sensitivity sweep 打破這個假設：高 contention 下 weight 選擇能把 mean JCT 移動 28%。M9 補上動態工具，分為兩個交付：**sim offline（2026-05-12）**與 **live service（2026-05-13）**。

### 8.1 為什麼 UCB1 而不是 LinUCB / PPO

兩個從 M8 推出來的結論：

1. Per-context oracle 跟 static best 在這個 workload 池只差 2.5%。Contextual policy 上限就是 oracle，最多再拿 2.5%，不值得為此加 ridge regression 或 gym env。
2. UCB1 跑 120 sim rounds 就拿到與 M8 grid-best 差 3% 以內的答案（grid 用 375 rounds）。**Bandit 的真正價值是 sample efficiency，不是 contextual adaptation**。

LinUCB eval JCT 2.745h > UCB1 2.587h，印證上述。LinUCB 留在 `bandit.py` 供 M10-F D-LinUCB 升級路徑使用，不是現行 production tuner。

### 8.2 演算法

`services/weight_tuner/bandit.py::UCB1Policy`。標準 UCB1（Auer et al. 2002）：

```
select:  argmax_a  μ_a + c · sqrt(2·ln(t) / n_a)   (untried arms → +∞)
update:  μ_a += (r − μ_a) / n_a ;  t += 1
```

- **Arm 空間**：`(α, δ, ε)` 3×3×3 grid = 27 arms；β=0.20 固定。見 `sim_env.py::default_arm_grid()`。
- **Reward（sim）**：`−jct_mean / 3600`，每 round 呼叫 `sim.runner.run()` 拿到。
- **Reward（live）**：`−mean_JCT_hours`，每 `COLLECTOR_INTERVAL_S` 秒從 slurmrestd 拉 completed jobs 計算。
- c=1.0（live serve.py），c=0.4（原 sim driver）。

### 8.3 f_pred_runtime 接線（2026-05-13）

原本 `f_pred_runtime` 是 stub，回傳固定 0.5（ε 係數無效）。M9 live 實作後改為：

```lua
function f_pred_runtime(job_desc)
  if not PRED_ENABLED then return 0.5 end
  local ok, success, pred_s = pcall(call_predictor, job_desc)
  if not ok or not success or not pred_s or pred_s <= 0 then return 0.5 end
  return clamp01(1.0 - pred_s / PRED_FALLBACK_SECONDS)
end
```

- 短 job（pred_s → 0）→ f_p → 1.0（高分，SJF-inspired）
- 長 job（pred_s → PRED_FALLBACK_SECONDS）→ f_p → 0.0
- predictor 不可用 → 0.5（中性，不影響 arm 間比較）

Live 驗證（`sleep 3` job，pred_s ≈ 180s，PRED_FALLBACK = 14400s）：
```
f_p = 1 − 180/14400 = 0.987 ≈ 0.99
score = 0.10·1.00 + 0.20·0.50 + 0.30·0.99 = 0.497  (vs stub 時 0.200)
```

### 8.4 Live service（weight-tuner）

`services/weight_tuner/serve.py`，FastAPI :8003，與 M10-D rl-scheduler 平行部署：

| endpoint | 說明 |
|---|---|
| `GET /weights` | 當前 UCB1 best arm `{arm:[α,δ,ε], alpha, delta, epsilon, beta, n_pulls, total_t}` |
| `POST /feedback` | `{arm, reward}` → UCB1.update；state 寫 `/state/ucb1_state.json` |
| `GET /stats` | 27 arms 的 n / mean reward |
| `GET /healthz` | liveness probe |

Lua 在 plugin load 時呼叫一次 `GET /weights`，pcall 保護，失敗 fallback chart 預設。
Background asyncio task 每 300s 拉 slurmrestd completed jobs，自動 POST /feedback。

**Helm**：`weightTuner.enabled=true/false`（預設 false），`weightTuner.lua.enabled` 控制 Lua 端。
NetworkPolicy：controller egress → weight-tuner:8003；weight-tuner egress → slurmrestd:6820 + DNS。

### 8.5 Sim + Live 結果對照

| 場景 | arm (α,δ,ε) | score | 備註 |
|---|---|---|---|
| M8 chart 預設 | (0.40, 0.20, 0.00) | 0.500 | f_pred_runtime=stub |
| M9 sim best | (0.10, 0.05, 0.60) | — | sim-only，ε=0.6 predictor-heavy |
| M9 live round 1 | (0.10, 0.05, 0.00) | 0.200 | UCB1 初始探索，f_p=stub |
| **M9 live round 2** | **(0.10, 0.05, 0.30)** | **0.497** | f_p=0.99（predictor 接通） |

### 8.6 Production rollout 流程

**離線快速更新**（每季）：拉 sacct → 重跑 `eval/scripts/run_m9_linucb.py` → 取 best arm → 更新 `chart/values.yaml scoreWeights` → `helm upgrade`。

**Live 自適應**（已部署）：`weight-tuner` service 持續運行，slurmrestd auto-feedback。
arm 收斂指標：top-3 arm pulls ≥ 60% total（需 GPU training workloads，~300 jobs 量級）。

**D-LinUCB 升級**（M10-F）：serve.py 切 `policy=linucb`，其餘架構不變。

## 9. Production 部署的三個必要條件

把 M3 + M5 + M9 接起來看，predictor 的 score 貢獻要能在 JCT 上量到，**三個條件必須同時成立**。這個 framework 也解釋了為什麼 sim 跑出 −20% 而 live e7 跑出 ~0%（詳見 `docs/eval-writeup.md` §5.5）。

### 條件 1 — Workload 要 heterogeneous

Job runtime 要跨多個量級。`f_runtime_short` 在 `pred_seconds` 接近並超過 `horizon` 時會飽和；如果 workload 是「所有 job 跑 60–360 秒」，函式整段卡在 [0.91, 0.98] 區間，spread 移不動 priority。

部署前快速確認：`sacct --format=Elapsed --starttime=-30days` 算 `p95 / p5`。比例小於 10× 的話，predictor 大概幫不上忙。

### 條件 2 — Predictor 在對齊的 distribution 上訓練

Bootstrap 模型（拿 synthetic Philly median 30 分鐘訓的）對實際跑 60–120 秒的 job 預測 766 秒，差 5–10×，signal 直接指錯方向。Chart 的 `runtimePredictor.retrain` CronJob 只要指向真實 sacct 歷史就能處理這個。確認方式：看 predictor 的 `/metrics`，`runtime_predictor_predict_total{mode="model"}` 是模型預測的次數、`mode="bootstrap"` 是 fallback 次數。如果 bootstrap 佔多數，predictor 等於沒用，要嘛重訓、要嘛等更多 sacct。

### 條件 3 — `horizon` 跟 `ε` 要跟 workload 跨度配

預設 `horizon=3600` 是當初鎖定 Philly synthetic 時挑的。對短 workload 來說太大、會把 spread 壓死。經驗法則：把 `horizon` 設成 workload 的 p50 runtime，這樣 `f_runtime_short` 落在 0.5 附近，整段 [0, 1] 動態範圍都能用來區分長短。

配套的 knob 是 `ε`。如果 `horizon` 不能動（例如多個 workload class 共用 cluster），就把 ε 加大來放大 priority spread。M9 在 synthetic 異質 Philly 上挑 ε=0.60，是 M3 預設的兩倍，就是這個道理。

### 條件 4（操作層面，不是 score function）— `applyTimeLimit=false`

嚴格說不算 score function 的條件，但放在這裡因為它是同一輪 E7 v2 學到的。Lua 的 `call_predictor` 預設 `applyTimeLimit=true`，會把 `job_desc.time_limit` 改寫成預測值。模型不準時，job 會在 walltime 被砍掉變 TIMEOUT。除非你已經有 calibrated predictor 加明確 walltime 預算需求，否則設 `false`。
