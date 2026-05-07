"""Train a LightGBM regressor on log-runtime.

The trainer reads a normalized job trace (the same JSON shape produced
by ``sim.loader.write_normalized``), splits 80/20 by submit timestamp
(time-honest hold-out — random split would leak future leaks of the same
user), trains a 200-tree LightGBM regressor, and writes a single
artifact bundle to disk:

    {
      "booster":           text-serialised lgbm Booster
      "feature_cols":      list[str]
      "user_stats":        {user_id: {"n": int, "mean_log_rt": float}}
      "train_metrics":     {"mae_log": float, "n_train": int, "n_test": int,
                            "p50_minutes_actual": float, ...}
      "model_version":     "lgbm-v<N>"
      "trained_at":        ISO-8601 string
    }

CronJob rotation: when ``--rotate`` is set and ``<output>`` already
exists, the trainer renames the existing artifact to ``<output>.bak``
before writing — exactly one previous model is kept (acceptance bullet
#3 in scheduler.md M5).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pickle
import shutil
import sys
from typing import Optional

import lightgbm as lgb
import numpy as np
import pandas as pd

from .features import FEATURE_COLS, aggregate_user_stats, build_training_frame


def _split_time_honest(df: pd.DataFrame, jobs: list, holdout_frac: float):
    n = len(df)
    cut = int(n * (1.0 - holdout_frac))
    train = df.iloc[:cut]
    test = df.iloc[cut:]
    train_jobs = jobs[:cut]
    return train, test, train_jobs


def train(
    trace_path: str,
    output: str,
    *,
    holdout_frac: float = 0.2,
    n_estimators: int = 200,
    rotate: bool = False,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    seed: int = 42,
) -> dict:
    with open(trace_path) as fh:
        jobs = json.load(fh)
    if not jobs:
        raise ValueError(f"trace {trace_path} is empty")

    jobs = sorted(jobs, key=lambda j: j["submit_ts"])
    df = build_training_frame(jobs)
    train_df, test_df, train_jobs = _split_time_honest(df, jobs, holdout_frac)
    if len(train_df) < 50 or len(test_df) < 5:
        raise ValueError(
            f"too few samples for time-honest split: "
            f"n_train={len(train_df)} n_test={len(test_df)}"
        )

    X_train = train_df[FEATURE_COLS]
    y_train = train_df["log_runtime"]
    X_test = test_df[FEATURE_COLS]
    y_test = test_df["log_runtime"]

    model = lgb.LGBMRegressor(
        objective="regression_l1",
        n_estimators=n_estimators,
        num_leaves=num_leaves,
        learning_rate=learning_rate,
        random_state=seed,
        n_jobs=-1,
        verbose=-1,
    )
    model.fit(X_train, y_train)

    pred = model.predict(X_test)
    mae_log = float(np.mean(np.abs(pred - y_test)))
    rmse_log = float(np.sqrt(np.mean((pred - y_test) ** 2)))

    user_stats = aggregate_user_stats(train_jobs)
    metrics = {
        "mae_log": mae_log,
        "rmse_log": rmse_log,
        "n_train": int(len(train_df)),
        "n_test": int(len(test_df)),
        "p50_minutes_actual": float(np.expm1(float(y_test.median())) / 60.0),
        "p50_minutes_pred": float(np.expm1(float(np.median(pred))) / 60.0),
    }

    bundle = {
        "booster": model.booster_.model_to_string(),
        "feature_cols": FEATURE_COLS,
        "user_stats": {
            u: {"n": s.n, "mean_log_rt": s.mean_log_rt}
            for u, s in user_stats.items()
        },
        "train_metrics": metrics,
        "model_version": _next_version(output),
        "trained_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    }

    os.makedirs(os.path.dirname(output) or ".", exist_ok=True)
    if rotate and os.path.exists(output):
        shutil.move(output, output + ".bak")
    with open(output, "wb") as fh:
        pickle.dump(bundle, fh)

    return metrics


def _next_version(output: str) -> str:
    if not os.path.exists(output):
        return "lgbm-v1"
    try:
        with open(output, "rb") as fh:
            prev = pickle.load(fh)
        prev_v = prev.get("model_version", "lgbm-v0")
        n = int(prev_v.rsplit("v", 1)[-1]) + 1
        return f"lgbm-v{n}"
    except Exception:
        return "lgbm-v1"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="runtime-predictor.train")
    p.add_argument("--trace", required=True,
                   help="normalized job trace JSON (sim.loader format)")
    p.add_argument("--output", required=True, help="model artifact path (.pkl)")
    p.add_argument("--holdout-frac", type=float, default=0.2)
    p.add_argument("--n-estimators", type=int, default=200)
    p.add_argument("--rotate", action="store_true",
                   help="rename existing <output> to <output>.bak before writing")
    p.add_argument("--mae-threshold", type=float, default=1.0,
                   help="fail with rc=1 if hold-out MAE exceeds this")
    args = p.parse_args(argv)

    metrics = train(
        args.trace,
        args.output,
        holdout_frac=args.holdout_frac,
        n_estimators=args.n_estimators,
        rotate=args.rotate,
    )
    print(json.dumps(metrics))
    if metrics["mae_log"] > args.mae_threshold:
        print(
            f"FAIL: mae_log={metrics['mae_log']:.4f} > threshold {args.mae_threshold}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
