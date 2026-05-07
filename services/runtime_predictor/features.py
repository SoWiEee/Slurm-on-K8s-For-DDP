"""Feature extraction for the runtime predictor.

Inputs are normalized job dicts (the same schema produced by ``sim.loader``
and consumed by the simulator). Outputs are pandas DataFrames whose
columns are stable across `train.py` and `app.py` — the trained model
artifact pickles the column order alongside the booster so inference
reuses exactly the same layout.

Features (kept deliberately small — LightGBM handles tabular sparsity
well, and we want the service to fit on a single CPU at low latency):

  - ``gpu_count``           int
  - ``mps_req``             int    [1, 4]
  - ``hour_of_week``        int    0–167  (from submit_ts; bootstraps fall to 0)
  - ``user_freq``           int    count of prior runs by this user in trace
  - ``user_mean_log_rt``    float  rolling mean of log(rt+1) for prior runs
  - ``gpu_type_*``          one-hot over a fixed alphabet (rtx4070/v100/p100/a10/h100/other)
  - ``partition_*``         one-hot over a fixed alphabet (cpu/gpu/debug/other)

The ``user_*`` columns are computed in *trace order* — for offline
training this means rolling stats up to (but excluding) the current job;
at inference time the service caches the latest per-user aggregates and
uses them as the lookup.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

GPU_TYPES = ("rtx4070", "v100", "p100", "a10", "h100", "other")
PARTITIONS = ("cpu", "gpu", "debug", "other")

NUMERIC_COLS = ["gpu_count", "mps_req", "hour_of_week", "user_freq", "user_mean_log_rt"]
GPU_TYPE_COLS = [f"gpu_type_{g}" for g in GPU_TYPES]
PARTITION_COLS = [f"partition_{p}" for p in PARTITIONS]
FEATURE_COLS = NUMERIC_COLS + GPU_TYPE_COLS + PARTITION_COLS


@dataclass(frozen=True)
class UserStats:
    n: int
    mean_log_rt: float


def _bucket(value: str, alphabet: Sequence[str]) -> str:
    return value if value in alphabet else "other"


def _onehot_row(value: str, alphabet: Sequence[str]) -> List[int]:
    bucket = _bucket(value, alphabet)
    return [1 if a == bucket else 0 for a in alphabet]


def _hour_of_week(submit_ts: float) -> int:
    # Treat submit_ts as Unix epoch (or simulator-relative seconds — both
    # work since we only need a stable mod-168 bucket).
    if submit_ts is None or not np.isfinite(submit_ts):
        return 0
    return int((submit_ts // 3600) % 168)


# ---------------------------------------------------------------------------
# Training: build a frame with rolling per-user stats
# ---------------------------------------------------------------------------
def build_training_frame(jobs: Iterable[dict]) -> pd.DataFrame:
    """Returns a DataFrame with FEATURE_COLS + ``log_runtime`` (target)."""
    jobs = sorted(jobs, key=lambda j: j["submit_ts"])
    user_n: dict = {}
    user_sum_log_rt: dict = {}

    rows: List[List] = []
    targets: List[float] = []
    for j in jobs:
        user = j.get("user", "anon")
        n = user_n.get(user, 0)
        mean_log = (user_sum_log_rt.get(user, 0.0) / n) if n > 0 else 0.0
        gpu_type = _bucket(str(j.get("gpu_type", "other")).lower(), GPU_TYPES)
        partition = _bucket(str(j.get("partition", "gpu")).lower(), PARTITIONS)
        row = [
            int(j["gpu_count"]),
            int(j.get("mps_req", 4)),
            _hour_of_week(float(j.get("submit_ts", 0.0))),
            n,
            mean_log,
        ]
        row.extend(_onehot_row(gpu_type, GPU_TYPES))
        row.extend(_onehot_row(partition, PARTITIONS))
        rows.append(row)
        target = float(np.log1p(max(0.0, float(j["runtime"]))))
        targets.append(target)

        # update rolling stats AFTER recording (so each row sees only prior)
        user_n[user] = n + 1
        user_sum_log_rt[user] = user_sum_log_rt.get(user, 0.0) + target

    df = pd.DataFrame(rows, columns=FEATURE_COLS)
    df["log_runtime"] = targets
    return df


# ---------------------------------------------------------------------------
# Inference: a single request → 1×N feature row
# ---------------------------------------------------------------------------
def make_inference_row(req: dict, user_stats: Optional[UserStats]) -> pd.DataFrame:
    n = user_stats.n if user_stats else 0
    mean_log = user_stats.mean_log_rt if user_stats else 0.0
    gpu_type = _bucket(str(req.get("gpu_type", "other")).lower(), GPU_TYPES)
    partition = _bucket(str(req.get("partition", "gpu")).lower(), PARTITIONS)
    row = [
        int(req.get("gpu_count", 1)),
        int(req.get("mps_req", 4)),
        _hour_of_week(float(req.get("submit_ts", 0.0))),
        n,
        mean_log,
    ]
    row.extend(_onehot_row(gpu_type, GPU_TYPES))
    row.extend(_onehot_row(partition, PARTITIONS))
    return pd.DataFrame([row], columns=FEATURE_COLS)


def aggregate_user_stats(jobs: Iterable[dict]) -> dict:
    """Compute final per-user (n, mean_log_rt) — used to seed the service."""
    n: dict = {}
    s: dict = {}
    for j in jobs:
        user = j.get("user", "anon")
        n[user] = n.get(user, 0) + 1
        s[user] = s.get(user, 0.0) + float(np.log1p(max(0.0, float(j["runtime"]))))
    return {u: UserStats(n=n[u], mean_log_rt=s[u] / n[u]) for u in n}
