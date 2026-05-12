"""Training pipeline + artifact rotation acceptance."""
from __future__ import annotations

import json
import os
import pickle
import sys
import tempfile

import pytest

# Reuse the synthetic Philly-like generator from M4 so both milestones
# train against the same trace shape.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
from sim.loader import generate_philly_like  # noqa: E402

from runtime_predictor.train import train  # noqa: E402


@pytest.fixture(scope="module")
def trace_path(tmp_path_factory):
    """Trace where runtime depends on gpu_count + user (mirrors Philly).

    Pure ``generate_philly_like`` draws runtime from a feature-independent
    log-normal — there is no signal for the regressor to learn, so MAE on
    log-runtime sits at the spread of the log-normal itself (~1.1). Real
    Philly has strong gpu_count and user correlations; we inject the same
    structure here so the < 1.0 MAE acceptance bullet is meaningful.
    """
    import random
    rng = random.Random(99)
    jobs = generate_philly_like(n_jobs=800, seed=42)
    base_by_gpu = {1: 600.0, 2: 1800.0, 4: 5400.0, 8: 14400.0}
    user_factor = {f"u{i:02d}": 0.4 + (i * 0.13) % 1.6 for i in range(40)}
    payload = []
    for j in jobs:
        d = j.as_dict()
        d["partition"] = "gpu" if d["gpu_count"] > 1 else "debug"
        base = base_by_gpu[d["gpu_count"]]
        uf = user_factor.get(d["user"], 1.0)
        # σ=0.4 multiplicative noise ⇒ log-space MAE floor ≈ 0.32
        d["runtime"] = float(base * uf * rng.lognormvariate(0, 0.4))
        payload.append(d)
    p = tmp_path_factory.mktemp("trace") / "trace.json"
    p.write_text(json.dumps(payload))
    return str(p)


def test_train_meets_mae_threshold(trace_path, tmp_path):
    out = tmp_path / "model.pkl"
    metrics = train(trace_path, str(out), n_estimators=200)
    assert out.exists()
    assert metrics["n_train"] >= 100
    # Acceptance bullet: hold-out MAE on log(runtime+1) < 1.0
    assert metrics["mae_log"] < 1.0, metrics


def test_train_artifact_has_required_fields(trace_path, tmp_path):
    out = tmp_path / "model.pkl"
    train(trace_path, str(out), n_estimators=50)
    with open(out, "rb") as fh:
        bundle = pickle.load(fh)
    for key in ("booster", "feature_cols", "user_stats",
                "train_metrics", "model_version", "trained_at"):
        assert key in bundle, f"missing {key}"
    assert bundle["model_version"].startswith("lgbm-v")


def test_rotate_keeps_exactly_one_backup(trace_path, tmp_path):
    out = tmp_path / "model.pkl"
    train(trace_path, str(out), n_estimators=50, rotate=True)  # v1
    train(trace_path, str(out), n_estimators=50, rotate=True)  # v2 -> .bak = v1
    train(trace_path, str(out), n_estimators=50, rotate=True)  # v3 -> .bak = v2

    bak = tmp_path / "model.pkl.bak"
    assert out.exists()
    assert bak.exists()
    with open(out, "rb") as fh:
        head = pickle.load(fh)
    with open(bak, "rb") as fh:
        prev = pickle.load(fh)
    assert head["model_version"] == "lgbm-v3"
    assert prev["model_version"] == "lgbm-v2"


def test_train_fails_on_empty_trace(tmp_path):
    p = tmp_path / "empty.json"
    p.write_text("[]")
    with pytest.raises(ValueError):
        train(str(p), str(tmp_path / "m.pkl"))
