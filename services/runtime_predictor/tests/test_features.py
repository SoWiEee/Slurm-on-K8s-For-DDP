"""Feature extraction shape, ordering, and rolling-stat correctness."""
from __future__ import annotations

import numpy as np

from runtime_predictor.features import (
    FEATURE_COLS,
    GPU_TYPES,
    PARTITIONS,
    aggregate_user_stats,
    build_training_frame,
    make_inference_row,
)


def _job(job_id, user, runtime, *, gpu=1, mps=4, gtype="rtx4070",
         partition="gpu", submit_ts=0.0):
    return {
        "job_id": job_id, "user": user, "gpu_count": gpu, "mps_req": mps,
        "gpu_type": gtype, "partition": partition,
        "submit_ts": submit_ts, "runtime": runtime,
    }


def test_feature_columns_are_stable_across_calls():
    jobs = [_job(f"j{i}", f"u{i%3}", 100 + i, submit_ts=i) for i in range(20)]
    df = build_training_frame(jobs)
    assert list(df.columns) == FEATURE_COLS + ["log_runtime"]
    # numeric block first, then one-hots
    assert df.iloc[0][FEATURE_COLS].notna().all()


def test_user_freq_is_rolling_not_total():
    jobs = [
        _job("a", "alice", 100, submit_ts=1),
        _job("b", "alice", 200, submit_ts=2),
        _job("c", "bob",   300, submit_ts=3),
        _job("d", "alice", 400, submit_ts=4),
    ]
    df = build_training_frame(jobs)
    # row 0 sees no prior alice; row 1 sees 1; row 3 (alice's 3rd) sees 2
    assert df.iloc[0]["user_freq"] == 0
    assert df.iloc[1]["user_freq"] == 1
    assert df.iloc[2]["user_freq"] == 0  # bob fresh
    assert df.iloc[3]["user_freq"] == 2
    # rolling mean for alice's 2nd run: log1p(100) only
    assert abs(df.iloc[1]["user_mean_log_rt"] - np.log1p(100)) < 1e-6


def test_gpu_type_and_partition_one_hot_collapses_to_other():
    jobs = [_job("x", "u", 600, gtype="b200", partition="weird", submit_ts=0)]
    df = build_training_frame(jobs)
    row = df.iloc[0]
    assert row["gpu_type_other"] == 1
    assert row["partition_other"] == 1
    # exactly one one-hot per group is set
    gtype_sum = sum(row[f"gpu_type_{g}"] for g in GPU_TYPES)
    part_sum = sum(row[f"partition_{p}"] for p in PARTITIONS)
    assert gtype_sum == 1 and part_sum == 1


def test_inference_row_matches_training_columns_for_unknown_user():
    inf = make_inference_row(
        {"user": "stranger", "gpu_count": 2, "mps_req": 4,
         "gpu_type": "rtx4070", "partition": "gpu", "submit_ts": 0.0},
        user_stats=None,
    )
    assert list(inf.columns) == FEATURE_COLS
    assert inf.iloc[0]["user_freq"] == 0
    assert inf.iloc[0]["user_mean_log_rt"] == 0.0
    assert inf.iloc[0]["gpu_type_rtx4070"] == 1


def test_aggregate_user_stats_matches_rolling_endpoint():
    jobs = [
        _job("a", "alice", 100, submit_ts=1),
        _job("b", "alice", 200, submit_ts=2),
        _job("c", "alice", 300, submit_ts=3),
    ]
    stats = aggregate_user_stats(jobs)
    expected = float(np.mean([np.log1p(x) for x in (100, 200, 300)]))
    assert stats["alice"].n == 3
    assert abs(stats["alice"].mean_log_rt - expected) < 1e-6
