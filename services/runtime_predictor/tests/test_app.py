"""FastAPI surface tests + p95 latency budget for /predict."""
from __future__ import annotations

import importlib
import json
import os
import pickle
import sys
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../..")))
from sim.loader import generate_philly_like  # noqa: E402

from runtime_predictor.train import train  # noqa: E402


@pytest.fixture(scope="module")
def trained_model(tmp_path_factory):
    import random
    rng = random.Random(99)
    jobs = generate_philly_like(n_jobs=800, seed=42)
    base_by_gpu = {1: 600.0, 2: 1800.0, 4: 5400.0, 8: 14400.0}
    user_factor = {f"u{i:02d}": 0.4 + (i * 0.13) % 1.6 for i in range(40)}
    payload = []
    for j in jobs:
        d = j.as_dict()
        d["partition"] = "gpu"
        d["runtime"] = float(
            base_by_gpu[d["gpu_count"]]
            * user_factor.get(d["user"], 1.0)
            * rng.lognormvariate(0, 0.4)
        )
        payload.append(d)
    trace = tmp_path_factory.mktemp("t") / "trace.json"
    trace.write_text(json.dumps(payload))
    model_path = tmp_path_factory.mktemp("m") / "model.pkl"
    train(str(trace), str(model_path), n_estimators=200)
    return str(trace), str(model_path)


@pytest.fixture
def client_with_model(trained_model, monkeypatch):
    trace, model_path = trained_model
    monkeypatch.setenv("MODEL_PATH", model_path)
    monkeypatch.setenv("MIN_TRAIN_SAMPLES", "100")
    # re-import so module-level HOLDER picks up env
    if "runtime_predictor.app" in sys.modules:
        del sys.modules["runtime_predictor.app"]
    app_mod = importlib.import_module("runtime_predictor.app")
    return TestClient(app_mod.app), trace


@pytest.fixture
def client_bootstrap(monkeypatch, tmp_path):
    monkeypatch.setenv("MODEL_PATH", str(tmp_path / "missing.pkl"))
    monkeypatch.setenv("MIN_TRAIN_SAMPLES", "100")
    if "runtime_predictor.app" in sys.modules:
        del sys.modules["runtime_predictor.app"]
    app_mod = importlib.import_module("runtime_predictor.app")
    return TestClient(app_mod.app)


def test_healthz_and_metrics_always_respond(client_bootstrap):
    r = client_bootstrap.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}
    r = client_bootstrap.get("/metrics")
    assert r.status_code == 200
    assert b"runtime_predictor_predict_total" in r.content


def test_predict_in_bootstrap_returns_capped_fallback(client_bootstrap):
    r = client_bootstrap.post("/predict", json={"user": "x", "user_time_limit_seconds": 999999})
    assert r.status_code == 200
    body = r.json()
    assert body["bootstrap"] is True
    assert body["model_version"] == "bootstrap"
    assert body["pred_seconds"] == 4 * 3600.0  # capped
    # tighter cap when user --time is shorter
    r2 = client_bootstrap.post("/predict", json={"user": "x", "user_time_limit_seconds": 600})
    assert r2.json()["pred_seconds"] == 600.0


def test_predict_uses_model_when_loaded(client_with_model):
    client, _ = client_with_model
    r = client.get("/readyz")
    assert r.json()["ready"] is True
    body = client.post("/predict", json={
        "user": "u00", "partition": "gpu",
        "gpu_count": 1, "mps_req": 4,
        "gpu_type": "rtx4070",
    }).json()
    assert body["bootstrap"] is False
    assert body["model_version"].startswith("lgbm-v")
    assert 60.0 < body["pred_seconds"] < 24 * 3600


def test_predict_p95_under_50ms(client_with_model):
    """Acceptance bullet: /predict p95 < 50ms.

    Run 200 in-process requests through the TestClient and assert the
    95th percentile end-to-end latency. TestClient adds some overhead vs
    a real ASGI server (no kernel sockets), so this is a *generous*
    upper bound on production latency, not a tight one.
    """
    client, _ = client_with_model
    payload = {"user": "u00", "gpu_count": 1, "mps_req": 4, "gpu_type": "rtx4070"}
    # warm the booster / pandas paths
    for _ in range(20):
        client.post("/predict", json=payload)
    samples = []
    for _ in range(200):
        t = time.perf_counter()
        r = client.post("/predict", json=payload)
        samples.append((time.perf_counter() - t) * 1000.0)
        assert r.status_code == 200
    p95 = float(np.percentile(samples, 95))
    p50 = float(np.percentile(samples, 50))
    assert p95 < 50.0, f"p50={p50:.2f}ms p95={p95:.2f}ms (samples n={len(samples)})"


def test_retrain_endpoint_swaps_model(client_with_model, tmp_path):
    client, trace = client_with_model
    new_out = tmp_path / "new_model.pkl"
    r = client.post("/retrain", json={
        "trace_path": trace,
        "output": str(new_out),
        "n_estimators": 50,
        "rotate": False,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["metrics"]["mae_log"] < 1.0
    assert body["model_version"].startswith("lgbm-v")
    assert new_out.exists()
