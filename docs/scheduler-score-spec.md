# Score Function Spec — Phase 6 M2

> **Status**: M2 scaffold (signed off 2026-05-06). Factors are **specified**
> here but **not yet computed** — `job_submit.lua` ships as a scaffold that
> only logs job_desc fields and (optionally) hard-codes priority for
> end-to-end plumbing tests. M3 fills in `f_mps_fit / f_vram_fit /
> f_fragmentation`; M5+M6 wire `f_pred_runtime`; M7 wires `f_topology`.

## 1. Purpose

A single scalar `score(J, P)` that ranks a (job, partition) candidate at
submit time. Higher score = better placement. Slurm's built-in priority
already orders jobs across the queue; the score plugs into that priority
multiplicatively so we can A/B "stock Slurm" vs "stock + score" without
re-implementing fairshare / age / partition factors.

## 2. Formula

```
score(J, P) = α · f_mps_fit(J, P)        ∈ [0, 1]
            + β · f_vram_fit(J, P)       ∈ [0, 1]
            + γ · f_topology(J, P)       ∈ [0, 1]
            - δ · f_fragmentation(J, P)  ∈ [0, 1]
            + ε · f_pred_runtime(J)      ∈ [0, 1]
```

Each `f_*` returns a normalised value in `[0, 1]`. The whole expression
is then clamped to `[0, 1]` and applied as:

```
job_desc.priority = base_priority + round(SCORE_GAIN · score)
```

where `base_priority` is what Slurm's built-in multifactor priority
already produces, and `SCORE_GAIN` is a chart value (default `1000`,
roughly equal to `PriorityWeightAge / hour`).

### Coefficients (initial, hand-tuned)

| Factor             | Symbol | Default | Sensitivity (M8 sweep range) |
|--------------------|:------:|:-------:|:----------------------------:|
| MPS fit            |   α    |  0.40   | 0.20–0.60                    |
| VRAM fit           |   β    |  0.20   | 0.10–0.30                    |
| Topology           |   γ    |  0.15   | 0.00–0.30                    |
| Fragmentation cost |   δ    |  0.20   | 0.10–0.40                    |
| Predicted runtime  |   ε    |  0.05   | 0.00–0.20                    |
|                    |        |         |                              |
| α + β + γ + ε      |   —    |  0.80   | bounded so δ can subtract    |

Coefficients live in `chart/values.yaml: slurm.jobSubmit.scoreWeights`
(M3 onwards). Any change in production must be paired with a sensitivity
analysis entry in this doc — append to §6.

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

### 3.3 `f_topology(J, P)` — NCCL collective affinity

Prefers nodes that share a fast interconnect for multi-node DDP jobs.

| Field          | Definition                                                                 |
|----------------|----------------------------------------------------------------------------|
| **Input**      | `job_desc.min_nodes`; node Feature labels (`net2-10g`, `net2-25g`); pool `topology` group from `worker-pools.json` |
| **Output**     | `1.0` if all candidate nodes share the same `topology` group; `0.0` if disjoint |
| **Formula**    | `count(same_group) / job.min_nodes`                                        |
| **Edge case**  | `min_nodes == 1` → `1.0` (single-node job has no collective)               |
| **M7 owner**   | Deferred — needs Multus + per-pool `topology` label first                  |

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

### 3.5 `f_pred_runtime(J)` — runtime prediction reward

Shorter jobs ranked slightly higher to improve Job Completion Time (JCT)
under SJF intuition. Predicted by the M5 LightGBM service.

| Field          | Definition                                                                 |
|----------------|----------------------------------------------------------------------------|
| **Input**      | HTTP POST to predictor service `/predict` with `job_desc` features         |
| **Output**     | `1.0` for predicted ≤ 5 min; decreases linearly to `0.0` at 8 hours        |
| **Formula**    | `max(0, 1 - log10(pred_minutes) / log10(480))`                             |
| **Edge case**  | Predictor timeout (>50ms) → `0.5` (neutral)                                |
| **Edge case**  | Predictor disabled (`slurm.predictor.enabled=false`) → `0.5`               |
| **M5/M6 owner**| Predictor service + lua HTTP client                                        |

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

### 4.2 Predictor HTTP contract (M5+M6)

```
POST /predict
  {
    "user": "alice",
    "partition": "gpu-rtx4070",
    "tres_per_node": "gpu:rtx4070:1,mps:25",
    "min_nodes": 1,
    "time_limit_minutes": 60,
    "features": ["vram-12g+"]
  }

  → 200 OK
  {"pred_minutes": 23.4, "p90_minutes": 41.0, "model_version": "lgbm-v3"}

  → timeout / 5xx
  treat as "no prediction" → f_pred_runtime returns 0.5
```

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
| 2026-05-06 | M3 (α=0.40, β=0.20, δ=0.20, γ=ε=0)    | sbatch 5-mix     |    n/a    | 27/27 lua unit + 79/79 helm-unittest green; live priorities matched manual compute (wholeNode=500, halfFrag=100, smallPack=50, twoTen=68, cpuOnly=500). Whole-node mps=100 dominates; mid-fraction (mps=50) penalised most by δ. |

(M4+ entries appended here as evaluation runs.)

## 7. Boundary policies

- **Plugin failure**: if `slurm_job_submit` raises a Lua error, slurmctld
  falls back to `slurm.SUCCESS` (no priority modification). Logged to
  `slurmctld.log` at level `error`.
- **Predictor unavailable**: `f_pred_runtime` returns 0.5 (see §3.5).
- **Score < 0** (δ dominates): clamp to 0; `job_desc.priority` is left
  untouched. Logged at level `info`.
- **Score > 1**: clamp to 1.
- **`SCORE_GAIN = 0`** (chart value): plugin still runs and logs but does
  not modify priority — useful for shadow-mode evaluation in M8.
