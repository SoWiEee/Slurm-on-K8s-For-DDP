"""Generate a workload-matched training trace for the M5 predictor.

The current bootstrap predictor (lgbm-v1) was trained on synthetic
Philly traces where median runtime is ~30 min. Our e7_jobs_hetero
workload runs jobs of 60-360s. Predictions are 5-10x off — so the
predictor's signal points the wrong way during live scheduling.

This script builds a training trace whose distribution matches what
the lua plugin will actually see at sbatch time:

  - users:   u05, u20, u01, u10, u34   (matching e7_jobs_hetero.sh)
  - per-user runtime: each user gets a characteristic runtime range,
    so user_mean_log_rt becomes informative
  - mps_req: 25 / 50 / 100  (matching e7 sizes)
  - many samples per user (~200) so user_stats has stable means

The user→runtime mapping is chosen to MATCH how e7_jobs_hetero.sh
assigns jobs to users in practice: small jobs (60-120s) go to
USERS[k%5] for k=0..11, mediums to k=12..17, larges to k=18,19.
Working out k%5:
  u05 (USERS[0]): k=0,5,10,15  -> 3 small + 1 medium
  u20 (USERS[1]): k=1,6,11,16  -> 3 small + 1 medium
  u01 (USERS[2]): k=2,7,12,17  -> 2 small + 2 medium
  u10 (USERS[3]): k=3,8,13,18  -> 2 small + 1 medium + 1 large
  u34 (USERS[4]): k=4,9,14,19  -> 2 small + 1 medium + 1 large
So u05/u20 are "fast user" leaning, u10/u34 are "slow user" leaning.
We reflect that in training so user_mean_log_rt encodes it.
"""
from __future__ import annotations

import json
import os
import random
import sys

OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/predictor_train_e7.json"

# Per-user (mean_seconds, stdev_log) — chosen to match the e7 workload's
# implicit user-runtime correlation. u05/u20 mostly run short; u10/u34
# mostly run longer. u01 is middle.
USER_PROFILES = {
    "u05": {"weight": 1.0, "samples": [(60,  90),  (60,  120), (90,  120)]},
    "u20": {"weight": 1.0, "samples": [(60,  90),  (60,  120), (120, 180)]},
    "u01": {"weight": 1.0, "samples": [(90,  120), (120, 180), (180, 300)]},
    "u10": {"weight": 1.0, "samples": [(120, 180), (180, 300), (240, 360)]},
    "u34": {"weight": 1.0, "samples": [(120, 180), (240, 360), (240, 360)]},
}

# A few extra unrelated users so the model doesn't just memorise 5
# bucket centroids — gives it variance to fit against.
EXTRA_USERS = {f"u{i:02d}": {"weight": 0.5,
                              "samples": [(60, 360)]}
               for i in (2, 3, 4, 7, 11, 25, 31)}

ALL_USERS = {**USER_PROFILES, **EXTRA_USERS}
N_TOTAL = 1500     # ~150/user — plenty for stable user_mean_log_rt
SEED = 42

rng = random.Random(SEED)
weights = [v["weight"] for v in ALL_USERS.values()]
names = list(ALL_USERS)
jobs = []

# Submit timestamps spread over 24h so hour_of_week varies a bit (the
# predictor weights this feature too).
t = 0.0
mean_gap = 24 * 3600.0 / N_TOTAL
for i in range(N_TOTAL):
    t += rng.expovariate(1.0 / mean_gap) if mean_gap > 0 else 0
    user = rng.choices(names, weights)[0]
    lo, hi = rng.choice(ALL_USERS[user]["samples"])
    runtime = rng.uniform(lo, hi)
    # mps_req: weight towards 25 since most e7 jobs are small
    mps = rng.choices([25, 50, 100], [0.60, 0.30, 0.10])[0]
    jobs.append({
        "job_id": f"e7t-{i:05d}",
        "user": user,
        "gpu_count": 1,
        "gpu_type": "rtx4070",
        "submit_ts": round(t, 3),
        "runtime": round(runtime, 3),
        "mem_req": 0.0,
        "mps_req": mps,
    })

with open(OUT, "w") as fh:
    json.dump(jobs, fh, indent=2)

# Quick distribution sanity report
import statistics
print(f"wrote {N_TOTAL} jobs → {OUT}")
print("per-user mean runtime (s):")
by_user = {u: [] for u in ALL_USERS}
for j in jobs:
    by_user[j["user"]].append(j["runtime"])
for u in sorted(by_user, key=lambda u: statistics.fmean(by_user[u]) if by_user[u] else 0):
    rts = by_user[u]
    if rts:
        print(f"  {u}: n={len(rts):3d}  mean={statistics.fmean(rts):5.0f}s "
              f"p50={sorted(rts)[len(rts)//2]:5.0f}s  p95={sorted(rts)[int(len(rts)*.95)]:5.0f}s")
