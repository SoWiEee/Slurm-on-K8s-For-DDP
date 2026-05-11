# Score Function Spec — Phase 6 (M2 → M9 final)

> **Status (2026-05-12)**: M3 + M5 + M6 + M9 evaluation complete; M7
> topology factor remains deferred (γ=0). Spec reflects what's actually
> running in `chart/templates/configmap-job-submit.yaml` and
> `sim/scheduler/score.py`, not the original M2 scaffold.
>
> **Milestone history**:
> - M2 (2026-05-06): lua scaffold only; every factor returns 0.5; coefficients hand-tuned.
> - M3 (2026-05-06): `f_mps_fit`, `f_vram_fit`, `f_fragmentation` activated.
> - M5/M6 (2026-05-07): runtime-predictor service + lua wire; `f_runtime_short` activated; ε flipped 0.05 → 0.30.
> - M7 (2026-05-10): fragmentation reconciler lives in operator (NOT a score factor); shadow mode default.
> - M8 (2026-05-11): cross-trace sensitivity sweep + paired CIs; γ stayed 0 (topology factor not implemented).
> - M9 (2026-05-12): UCB1 offline weight tuner (see §8); learnt arm matches grid-best within 3%.

## 1. Purpose

A single scalar `score(J, P)` that ranks a (job, partition) candidate at
submit time. Higher score = better placement. Slurm's built-in priority
already orders jobs across the queue; the score plugs into that priority
multiplicatively so we can A/B "stock Slurm" vs "stock + score" without
re-implementing fairshare / age / partition factors.

## 2. Formula

```
score(J, P) = α · f_mps_fit(J, P)         ∈ [0, 1]
            + β · f_vram_fit(J, P)        ∈ [0, 1]
            + γ · f_topology(J, P)        ∈ [0, 1]   (γ = 0; deferred)
            - δ · f_fragmentation(J, P)   ∈ [0, 1]
            + ε · f_runtime_short(J)      ∈ [0, 1]
```

Each `f_*` returns a normalised value in `[0, 1]`. The whole expression
is clamped to `[0, 1]` and applied as:

```
job_desc.priority = base_priority + round(SCORE_GAIN · score)
```

`base_priority` is Slurm's built-in multifactor priority output;
`SCORE_GAIN` is a chart value (default `1000`, roughly equal to
`PriorityWeightAge / hour` so a perfect score buys ~1 hour of age).

The lua function name in `job_submit.lua` is `f_pred_runtime` for
backwards compatibility; sim code uses `f_runtime_short`. Same factor.

### Coefficients (current production defaults, post-M5)

| Factor             | Symbol | Default | M8 sensitivity spread       | M9 UCB1 pick |
|--------------------|:------:|:-------:|:---------------------------:|:------------:|
| MPS fit            |   α    |  0.40   | 0.10–0.70 (jct_mean ±10.6%) |     0.10     |
| VRAM fit           |   β    |  0.20   | held fixed in M8/M9         |     0.20     |
| Topology           |   γ    | **0**   | not implemented             |     0        |
| Fragmentation cost |   δ    |  0.20   | 0.05–0.40 (jct_mean ±10.6%) |     0.05     |
| Predicted runtime  |   ε    |  0.30   | 0.00–0.60                   |     0.60     |

Coefficients live in `chart/values.yaml: slurm.jobSubmit.scoreWeights`.
Any change in production must be paired with a sensitivity entry in §6.

**Two observations from M8/M9** that the original M2 spec didn't anticipate:

1. **Workload-dependent sensitivity**. M8's 5×5 (α, δ) grid swept across
   three trace families: ali 0.1%, philly 10.6%, burst 28.5% mean-JCT
   spread. **Low-contention workloads see no benefit from weight tuning**;
   high-contention ones see > 25%. M9 UCB1 reflects this — it picks
   the burst-optimal arm because that's where the reward signal is.
2. **M9 picks a "predictor-heavy" arm** (α=0.10, ε=0.60) on the
   simulator's heterogeneous Philly trace. The default (α=0.40, ε=0.30)
   was hand-tuned before M5 was wired. M9's pick is the answer to
   "given the predictor exists, how should the other weights bend?" —
   make the predictor do more work, ease the mps_fit hammer.

## 3. Factor specifications

### 3.1 `f_mps_fit(J, P)` — MPS slot capacity fit

How well the job's MPS request fits into a single node's free MPS slots.

| Field          | Definition                                                                 |
|----------------|----------------------------------------------------------------------------|
| **Input**      | `job_desc.tres_per_node` (e.g. `"gpu:rtx4070:1,mps:25"`); per-node `mps_free` from `sinfo -O Gres` |
| **Output**     | `1.0` if exact-fit on **best** candidate node, decreasing as residual MPS grows |
| **Formula**    | `1 - residual_mps / 100` where `residual_mps = best_node.mps_free - job.mps_req` |
| **Edge case**  | `job.mps_req == 0` → `1.0` (no MPS request, no penalty)                    |
| **Edge case**  | No node satisfies `mps_free ≥ mps_req` → `0.0`                              |
| **M3 owner**   | `f_mps_fit` in `job_submit.lua`                                            |

### 3.2 `f_vram_fit(J, P)` — VRAM headroom

Penalises sending a tiny-VRAM job to a big-VRAM node when a smaller one
is available, and vice-versa.

| Field          | Definition                                                                 |
|----------------|----------------------------------------------------------------------------|
| **Input**      | `job_desc.features` constraint (e.g. `vram-12g+`); node Feature labels (`vram-12g`, `vram-24g`) |
| **Output**     | `1.0` when smallest viable node matches; `0.5` when one tier oversized; `0.0` when no tier fits |
| **Formula**    | `1 - (vram_node - vram_min) / max_vram` clamped to `[0, 1]`                 |
| **Edge case**  | No `vram-*` constraint → `0.5` (neutral)                                   |
| **Edge case**  | No node has any `vram-*` Feature → `0.5` (neutral, don't punish CPU jobs)  |
| **M3 owner**   | `f_vram_fit` in `job_submit.lua`                                           |

### 3.3 `f_topology(J, P)` — NCCL collective affinity *(deferred, γ=0)*

Prefers nodes that share a fast interconnect for multi-node DDP jobs.

| Field          | Definition                                                                 |
|----------------|----------------------------------------------------------------------------|
| **Input**      | `job_desc.min_nodes`; node Feature labels (`net2-10g`, `net2-25g`); pool `topology` group from `worker-pools.json` |
| **Output**     | `1.0` if all candidate nodes share the same `topology` group; `0.0` if disjoint |
| **Formula**    | `count(same_group) / job.min_nodes`                                        |
| **Edge case**  | `min_nodes == 1` → `1.0` (single-node job has no collective)               |
| **Status**     | **Not implemented**. lua returns 0.5 stub, γ=0 in production so it contributes nothing. The chart's gpu-rtx4070 / gpu-rtx4080 pools are single-node anyway, so multi-node DDP affinity has no test workload right now. Would need Multus + per-pool topology label + a multi-node DDP trace family before this is worth implementing. |

### 3.4 `f_fragmentation(J, P)` — post-placement fragmentation cost

Discourages placements that leave the cluster more fragmented (Gandiva
intuition: balanced free MPS across nodes is healthier than concentrated).

| Field          | Definition                                                                 |
|----------------|----------------------------------------------------------------------------|
| **Input**      | per-node `mps_free` (post-hypothetical-placement)                          |
| **Output**     | `0.0` → no fragmentation; `1.0` → maximally fragmented                      |
| **Formula**    | `stddev(mps_free) / 100` (each node has 100 MPS slots)                     |
| **Edge case**  | Single node → `0.0` (stddev undefined → no fragmentation cost)             |
| **M3 owner**   | `f_fragmentation` in `job_submit.lua`                                      |

### 3.5 `f_runtime_short(J)` — SJF kicker fed by M5 predictor

Shorter jobs get a higher score to improve mean JCT under SJF intuition.
Predicted runtime comes from the M5 LightGBM service. Lua function name
in the running chart is `f_pred_runtime` (kept for M2 compatibility);
sim code in `sim/scheduler/score.py` uses `f_runtime_short`.

| Field          | Definition                                                                 |
|----------------|----------------------------------------------------------------------------|
| **Input**      | `pred_seconds` from `POST /predict` (M5 service). Lua reads from `job_desc.user_name`, `tres_per_node`, `min_nodes`, `features`, `time_limit`. |
| **Formula**    | `f_runtime_short = horizon / (horizon + pred_seconds)`. Default `horizon=3600` s (1 hour). |
| **Output**     | `→ 1.0` for instant jobs; `0.5` at pred=horizon; `→ 0` for pred ≫ horizon. |
| **Saturation property** | `f` 跟 `pred_seconds` 的關係是 sub-linear，所以不會被一個極長預測值完全 dominate；spread 取決於 `pred_seconds / horizon` 的範圍。 |
| **Edge case**  | Predictor timeout (curl --max-time 200ms) → return `(false, nil, "timeout")`; lua leaves `priority` untouched; `f` 算作 0.5 (neutral). |
| **Edge case**  | Predictor disabled (`slurm.jobSubmit.predictor.enabled=false`) → `f=0.5` (neutral). |
| **Edge case**  | Predictor returns bootstrap (`model_version="bootstrap"`, n_train < MIN_TRAIN_SAMPLES) → 仍正常吃預測值（min(user_time_limit, 4h)），不切到 neutral；lua 透過 `bootstrap=true` flag 知道但不改行為。 |
| **Owner**      | `services/runtime_predictor/` + `chart/templates/configmap-job-submit.yaml::call_predictor()` |

**`horizon` 該怎麼設**：根據 M8/E7 觀察，`horizon` 應該跟 workload 的 p90 runtime 同量級。E7 上 workload 跨度只到 360 秒、horizon=3600 讓 `f` spread 從 0.937 到 0.976（差 0.04），predictor 信號被 ε 縮成 priority 上 12 點 ≈ 43 秒等待換算值，**完全淹沒在 noise 裡**。Production 要看到 sim 等級的改善，把 horizon 拉到接近 workload p50–p90 比較合理；或者 ε 加大。

**`applyTimeLimit` 預設要改成 false**：lua 的 `call_predictor` 預設 `applyTimeLimit=true`，會把 `job_desc.time_limit` 改寫成預測值。E7 v2 實驗發現如果模型對某個 user 預測 150 秒、但實際 job 跑 250 秒，Slurm 在 150 秒砍掉 job 變 TIMEOUT。**Production 部署除非有非常好的理由，否則把 chart value `slurm.jobSubmit.predictor.applyTimeLimit` 設成 `false`，只讓 predictor 做排序、不要動 walltime**。

## 4. I/O schema

### 4.1 `job_submit.lua` entry point

```lua
function slurm_job_submit(job_desc, part_list, submit_uid)
  -- read-only access:  job_desc.{name, partition, num_tasks, min_nodes,
  --                              tres_per_node, features, time_limit,
  --                              user_id, account, qos, ...}
  -- writable:          job_desc.{priority, time_limit, comment}
  -- return value:      slurm.SUCCESS or slurm.ERROR
end
```

### 4.2 Predictor HTTP contract (M5+M6, current production)

Lua builds the body inline (no JSON lib in the controller image); the
predictor's Pydantic schema is in `services/runtime_predictor/app.py::PredictRequest`.

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

Failure modes (lua handles all of these as "no prediction", f=0.5):

| Condition | lua sees | Behaviour |
|---|---|---|
| `curl --max-time 200ms` timeout | shell exit 28 | leaves `priority` untouched |
| HTTP 5xx (service down) | curl exit 0, body empty / non-JSON | `parse_predict_response` returns nil → no override |
| Body missing `pred_seconds` | parse_predict_response returns nil | no override |
| `slurm.jobSubmit.predictor.enabled=false` (chart) | lua skips the whole `call_predictor` block | f=0.5 stub |
| Predictor in bootstrap regime (n_train < 100) | `bootstrap=true` flag in response | lua still uses the value (it's `min(user_time_limit, 4h)`, sane) |

The `--max-time 200ms` budget is deliberately tight. slurmctld is
single-threaded over the lua plugin, so a hung predictor would slow
every sbatch by up to 200ms. In practice predictor latency is < 5ms p99
(measured on the e7 cluster — 108 predictions, p99 < 2ms).

## 5. Test cases (busted, M3+)

```
describe "f_mps_fit" {
  it "exact fit returns 1.0" {
    -- job mps=25, only node has mps_free=25 → 1.0
  }
  it "zero mps request returns 1.0" {
    -- no penalty for jobs not asking for MPS
  }
  it "no fitting node returns 0.0" {
    -- mps_req > all nodes' mps_free
  }
  it "monotonic in residual" {
    -- residual=10 < residual=50 ⇒ score(10) > score(50)
  }
}

describe "f_fragmentation" {
  it "single node returns 0.0" { ... }
  it "balanced free MPS returns ≈ 0.0" { ... }
  it "skewed free MPS returns ≥ 0.3" { ... }
}

describe "score (composite)" {
  it "default weights sum to ≤ 1" {
    -- α+β+γ+ε ≤ 1, δ subtracts so total ∈ [-δ, α+β+γ+ε]
  }
  it "fragmentation penalty subtracts" { ... }
}
```

## 6. Sensitivity analysis log

| Date       | Change                                | Workload         | JCT delta | Notes |
|------------|---------------------------------------|------------------|-----------|-------|
| 2026-05-06 | M2 scaffold (no factors active)       | verify.sh        |     0%    | Baseline; lua plugin loaded but every factor returns 0.5 |
| 2026-05-06 | M3 (α=0.40, β=0.20, δ=0.20, γ=ε=0)    | sbatch 5-mix     |    n/a    | 27/27 lua unit + 79/79 helm-unittest green; live priorities matched manual compute. Whole-node mps=100 dominates; mid-fraction (mps=50) penalised most by δ. |
| 2026-05-07 | M4 simulator baseline (α=0.40, β=0.20, δ=0.20) | Philly-like 1k @ 4×4 | jct_mean −0.7 % vs multifactor; **wait_p90 −20.8 %**; bf_rate +3.2 pp | Score kicker reordering small well-fit jobs ahead of head-of-line big jobs. |
| 2026-05-07 | M6 plumbing (predictor enabled, applyTimeLimit=true) | offline E2E (lua + curl + uvicorn) | n/a | lua → predictor round-trip 跑通；live mode 把 `time_limit=0` 改成 10 min，fallback mode 保持 0。 |
| 2026-05-11 | **M8 cross-trace** (α=0.40, β=0.20, δ=0.20, ε=0.30) | 3 traces × 5 seeds (Philly/burst/ali) | paired Δ vs vendor multifactor: **philly −20.1%、burst −28.7%、ali −0.08%** | 兩個 contention-heavy trace 上 statistically significant；ali（util 0.30）沒空間給 predictor 發揮。M5 確認是整套 stack 的主要 win。詳見 `docs/eval-writeup.md` §4。 |
| 2026-05-11 | **E6 5×5 sensitivity grid** (α ∈ {0.10..0.70}, δ ∈ {0.05..0.40}, ε=0.30) | same 3×5 | jct_mean spread: **ali 0.1%、philly 10.6%、burst 28.5%** | Weight sensitivity 跟 contention 正相關；best cells 落在 α ≤ 0.25、δ ≤ 0.15。**Workload-dependent**：低 contention cluster 不需要 tune。 |
| 2026-05-11 | M7 fragmentation reconciler (operator, NOT a score factor) | same 3×5 | paired Δ vs E4: **philly +33.1%、burst +60.9%、ali +6.0%** | **Net negative 跨 distribution 一致**。Victim-selection-by-priority 會踢掉跑了一半的 job、lost progress > 排程改善。E5b 對照組（ckpt cost=0）證實主因不是 reload overhead 而是 lost progress。M7 留 `shadowMode=true`、不進 production。 |
| 2026-05-11 | E7 live `our` (M3 score only) | k3s + 1 × RTX 4070, 20-job mix | paired Δ vs vendor: **−38.6%** （clean subset） | Headline −57.7% 被 vendor pass 的 AllocTRES freeze 撐高；乾淨 small-job 子集是 −38.6%，可進論文 main result。 |
| 2026-05-11 | E7 live `our_pred` (M3 + M5, bootstrap predictor on Philly synthetic) | same | vs `our`: −0.13% (noise) | Bootstrap 模型對 e7 workload distribution 不準（predict 766s、actual 60–120s），signal 指錯方向。 |
| 2026-05-12 | E7 live `our_pred_hetero_v2` (matched predictor, MAE_log 0.34) | same | vs `our` (homogeneous): −0.08% (still noise) | 模型對齊也救不回來——**workload runtime spread 只 6×，f_runtime_short 換算到 priority 只值 43 秒等待**。Predictor signal 被淹沒。**這指出 production 部署的第三個必要條件：horizon / ε 要跟 workload 跨度配**。 |
| 2026-05-12 | **M9 UCB1 weight tuner** (services/weight_tuner, offline) | 3 traces × 5 seeds, 27 arms, 120 rounds | eval JCT 2.587h vs M8 grid-best 2.511h (**+3.0%**, vs oracle 2.448h +5.7%) | Picked arm (α=0.10, δ=0.05, ε=0.60) — predictor-heavy. **省 1/3 sim 預算**（120 vs 375）。LinUCB 不如 UCB1，因為 oracle vs static-best 只差 2.5%，context 沒空間發揮。PPO 不做（同上限）。|

(Future runs appended here.)

## 7. Boundary policies

- **Plugin failure**: if `slurm_job_submit` raises a Lua error, slurmctld
  falls back to `slurm.SUCCESS` (no priority modification). Logged to
  `slurmctld.log` at level `error`.
- **Predictor unavailable**: `f_runtime_short` returns 0.5 (see §3.5).
- **Score < 0** (δ dominates): clamp to 0; `job_desc.priority` is left
  untouched. Logged at level `info`.
- **Score > 1**: clamp to 1.
- **`SCORE_GAIN = 0`** (chart value): plugin still runs and logs but does
  not modify priority — useful for shadow-mode evaluation in M8.

## 8. M9 — Bandit weight tuning (offline)

M2 spec assumed coefficients would stay hand-tuned forever. M8 sensitivity
sweep showed that's not optimal: under contention, weight choice can move
mean JCT by up to 28%. M9 closes the loop with an offline bandit.

### 8.1 Why offline (not online RL)

Two cheap conclusions from M8 ruled out the heavyweight version:

1. **Per-context oracle vs static best 只差 2.5%** across the workload pool.
   Contextual policies (LinUCB, PPO) can't exceed oracle, so their ceiling
   is +2.5% above the static best. Not worth a Stable-Baselines3 + custom
   gym + chart deployment.
2. **UCB1 with 120 sim runs gets within 3% of the M8 grid-best** which
   used 375 runs. So the bandit's value is **sample efficiency**, not
   contextual adaptation. Run it offline once per quarter against
   refreshed sacct, ship the picked weights, done.

### 8.2 Algorithm

`services/weight_tuner/bandit.py::UCB1Policy`. Standard UCB1:

```
arm a:  pull a ⇒ reward r;  μ_a += (r − μ_a)/n_a;  t += 1
select: argmax_a  μ_a + c · sqrt(2·ln(t) / n_a)
                                ^^^^^^^^^^^^^^^^^
                                UCB exploration bonus, c=0.4
```

- **Arm space**: `(α, δ, ε)` on a 3×3×3 grid (27 arms). β fixed at 0.20.
  See `services/weight_tuner/sim_env.py::default_arm_grid()`.
- **Reward**: `-jct_mean / 3600` from `sim.runner.run()` on a sampled
  (trace_family, seed). One round = one simulator invocation.
- **Pull caching**: `SimPull` memoises `(arm, context) → reward` so
  revisits are free; M9's reported 120 rounds means 120 unique
  (arm, context) pulls plus arbitrary cache hits.

### 8.3 LinUCB variant (for completeness)

`services/weight_tuner/bandit.py::LinUCBPolicy`. Disjoint per-arm ridge
regression with context = `(n_jobs/2000, mean_mps/4, mean_gpu/8)`.
Selection: `argmax_a θ_a · x + α · sqrt(x · A_a⁻¹ · x)`, α=0.6, ridge=1.0.

**Result**: LinUCB underperformed UCB1 (eval JCT 2.745 vs 2.587).
Context provides essentially no learnable signal here because the
per-context oracle is only 2.5% from the static best. LinUCB's ridge
regression spends its budget fitting that small signal and gets dragged
around by noise. Documented for reproducibility; not the recommended
production tuner.

### 8.4 What ships vs what doesn't

- **Ships in repo (this thesis)**: `services/weight_tuner/` (bandit
  policies + sim adapter + 6 unit tests). Offline driver in
  `eval/scripts/run_m9_linucb.py`.
- **Does NOT ship in the chart**: there's no `weightTuner.enabled` chart
  value, no Service, no PVC. M9 is a **offline analysis tool**, not a
  runtime component. The picked weights get committed to
  `chart/values.yaml` after each re-run.
- **Production rollout pattern** (recommended): every quarter or so,
  pull cluster sacct, regenerate a `--synth-jobs` training trace whose
  distribution matches reality, re-run `run_m9_linucb.py`, take the
  arm with the lowest eval JCT, update `slurm.jobSubmit.scoreWeights`
  in the chart, `helm upgrade`. Log the change in §6 with the JCT
  delta and the sacct snapshot date.

## 9. Production deployment — three necessary conditions

Putting M3 + M5 + M9 together, **three conditions need to hold simultaneously**
for the predictor's score contribution to move JCT in a measurable way.
This is the framework that explains why sim sees −20% and live e7 sees ~0%
(see `docs/eval-writeup.md` §5.5).

### Condition 1 — Workload heterogeneity

Job runtimes must span multiple orders of magnitude. `f_runtime_short`
saturates as `pred_seconds` approaches and exceeds `horizon`; if the
workload is "all jobs run 60–360s" the function never leaves the
[0.91, 0.98] band and the spread can't move priority.

Quick check before deploy: `sacct --format=Elapsed --starttime=-30days`,
compute `p95 / p5`. If < 10×, the predictor is unlikely to help.

### Condition 2 — Predictor trained on matched distribution

The bootstrap model (trained on synthetic Philly with median 30 min)
predicted 766s for jobs that actually ran 60–120s on the live cluster
— 5–10× off, signal pointing the wrong way. The chart's
`runtimePredictor.retrain` CronJob handles this if pointed at real
sacct history. Verify with the predictor's `/metrics`:
`runtime_predictor_predict_total{mode="model"}` increments per pull
and `mode="bootstrap"` for fallback. If mode=bootstrap dominates, the
predictor is effectively useless — retrain or wait for more sacct.

### Condition 3 — `horizon` and `ε` configured for workload scale

Default `horizon=3600` was hand-picked when Philly synthetic was the
target trace. For shorter workloads it crushes the spread. Rough rule
of thumb: set `horizon` to the workload's p50 runtime, so
`f_runtime_short` lives near 0.5 and has full [0, 1] dynamic range to
differentiate short vs long.

Companion knob: `ε`. If you can't move `horizon` (e.g. multiple workload
classes share the cluster), bump ε to widen the priority spread the
factor produces. M9 picked ε=0.60 on synthetic heterogeneous Philly —
double the M3 default — for exactly this reason.

### Condition 4 (operational, not score-function) — `applyTimeLimit=false`

Not a score-function condition strictly, but worth listing because it
came out of the same E7 v2 experiment. Lua's `call_predictor` defaults
to `applyTimeLimit=true`, which rewrites `job_desc.time_limit` with the
prediction. Under model mismatch this kills jobs at TIMEOUT before they
finish. Default to `false` unless you have a calibrated predictor and
explicit walltime budget enforcement requirements.
