# Trace replay simulator — Phase 6 M4

A pure-Python (stdlib-only) discrete-event simulator that drives a job
trace through a configurable cluster + scheduler and reports JCT,
makespan, utilization, slowdown, and a coarse backfill rate. Written so
the M3 score function can be evaluated offline against statistically
meaningful traces without needing a real cluster.

## Quick start

```bash
# 1. unit tests
python3 -m unittest discover -v sim.tests

# 2. one-shot acceptance run (1000 jobs × 3 schedulers)
bash scripts/verify-sim.sh

# 3. ad-hoc: generate a synthetic trace and run M3 score
python3 -m sim.runner \
  --synth-jobs 1000 --synth-seed 42 \
  --scheduler score --nodes 4 --gpus-per-node 4 \
  --output /tmp/score.csv --summary-json /tmp/score.json
```

Outputs land under `sim/data/out/{fcfs,multifactor,score}.{csv,json}`
when run via `verify-sim.sh`.

## Trace formats

`sim/loader.py` accepts two JSON shapes:

1. **Normalized** — list of dicts with keys
   `{job_id, user, gpu_count, gpu_type, submit_ts, runtime, mem_req, mps_req}`.
   `submit_ts` and `runtime` are seconds; `mps_req` is per-GPU MPS slots
   in `[1, MPS_PER_GPU]`. Whole-GPU jobs use `mps_req == MPS_PER_GPU`
   (default 4).

2. **Philly raw** — `cluster_log_data.json` from
   <https://github.com/msr-fiddle/philly-traces>. Auto-detected by the
   presence of `submitted_time` / `attempts`. The loader picks the
   longest successful attempt per job and uses its duration as
   `runtime`.

Philly does **not** carry MPS data (jobs ran in whole-GPU units). To
exercise the M3 score factors we *augment* a configurable fraction of
single-GPU jobs by lowering `mps_req` to a random value in `{1, 2, 3}`
(default 30 %, deterministic seed). All evaluation in §M8 will report
metrics with and without this augmentation.

## Schedulers

| Name           | What it does                                                         |
|----------------|----------------------------------------------------------------------|
| `fcfs`         | Strict submit-order; head-of-line blocking; no backfill              |
| `multifactor`  | Slurm `priority/multifactor` approximation (age + jobsize + qos)     |
| `score`        | `multifactor` + M3 score kicker (`α·mps_fit + β·vram_fit − δ·frag`)  |

Coefficients in `sim/scheduler/score.py` mirror the chart defaults in
`chart/values.yaml` under `slurm.jobSubmit.scoreWeights`. Keep them in
sync when running M8 sensitivity sweeps.

## Cluster model

`sim/cluster.py` models `n_nodes × gpus_per_node` GPUs, each GPU split
into `mps_per_gpu` MPS slots (default 4). Single-GPU MPS-fractional
jobs pack onto the GPU with the smallest matching residual; multi-GPU
jobs span nodes and prefer those with the largest free whole-GPU pool.
No preemption, no checkpoint awareness — those live in the live
operator (M7 will mirror them in-sim).

## Synthetic Philly-like generator

`generate_philly_like(n, seed)` produces a deterministic trace with
shape close to public Philly subsamples:

- GPU counts ∈ {1, 2, 4, 8} with weights `{0.75, 0.12, 0.10, 0.03}`
- Runtimes are log-normal (median ≈ 30 min, p95 ≈ 6 h)
- Submit timestamps are Poisson, mean rate tuned for ~5-day horizon
- 30 % of single-GPU jobs are MPS-fractional

Use this when the real Philly tar.gz is not available.

## Output schema

Per-job CSV columns:

```
job_id, user, gpu_count, mps_req,
submit_ts, start_ts, end_ts,
runtime, wait, jct, slowdown
```

Summary JSON keys (printed to stdout, optionally to `--summary-json`):

```
n_jobs, makespan, jct_{mean,p50,p90,p95},
wait_{mean,p90}, slowdown_{mean,p90},
utilization, bf_rate, scheduler, wall_seconds, nodes, gpus_per_node
```

## Acceptance baseline (2026-05-07, 1000-job synthetic, 4×4 cluster)

| Scheduler   | wall    | JCT mean | JCT p90  | wait p90 | util  | bf rate |
|-------------|---------|----------|----------|----------|-------|---------|
| fcfs        | 0.03 s  | 45 489 s | 69 014 s | 63 537 s | 0.836 | 0.000   |
| multifactor | 0.15 s  | 13 216 s | 34 976 s | 25 486 s | 0.935 | 0.912   |
| score       | 0.32 s  | 13 129 s | 31 281 s | 20 170 s | 0.926 | 0.941   |

`score` improves p90 wait by **−20.8 %** vs `multifactor` and lifts
backfill rate by **+3.2 pp**. JCT mean is statistically tied (the larger
gains land on the long-tail percentiles, as expected when the kicker
favours small well-fit jobs that would otherwise queue behind big
multi-GPU ones).
