"""FastAPI service for runtime prediction.

Endpoints:

  POST /predict     — body: PredictRequest; returns PredictResponse
  POST /retrain     — kicks off an in-process train.train(...) (used by
                      the chart's CronJob; protected by a shared secret
                      header in production — for the lab we leave it open
                      and rely on NetworkPolicy)
  GET  /healthz     — liveness; always 200 if the process is up
  GET  /readyz      — readiness; 200 only once a model is loaded *or*
                      bootstrap-with-prior is in effect
  GET  /metrics     — Prometheus exposition

Cold-start fallback (per scheduler.md M5 risk note):
  When ``MIN_TRAIN_SAMPLES`` (default 100) is not yet satisfied or the
  model file does not exist, ``/predict`` returns
  ``min(user_time_limit_seconds, 4*3600)`` and sets
  ``model_version="bootstrap"`` so callers can log the regime.
"""
from __future__ import annotations

import logging
import os
import pickle
import threading
import time
from typing import Dict, List, Optional

import lightgbm as lgb
import numpy as np
from fastapi import FastAPI, HTTPException
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field
from starlette.responses import Response

from . import features as feat
from .features import FEATURE_COLS, UserStats, make_inference_row

log = logging.getLogger("runtime-predictor")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
class PredictRequest(BaseModel):
    user: str = "anon"
    partition: str = "gpu"
    gpu_count: int = 1
    mps_req: int = 4
    gpu_type: str = "rtx4070"
    submit_ts: float = 0.0
    user_time_limit_seconds: Optional[float] = Field(
        default=None,
        description="user-supplied --time in seconds; used as the bootstrap "
                    "fallback ceiling (capped at 4 hours)",
    )


class PredictResponse(BaseModel):
    pred_seconds: float
    pred_minutes: float
    model_version: str
    bootstrap: bool
    latency_ms: float


class RetrainRequest(BaseModel):
    trace_path: str
    output: Optional[str] = None
    holdout_frac: float = 0.2
    n_estimators: int = 200
    rotate: bool = True


class RetrainResponse(BaseModel):
    ok: bool
    metrics: dict
    model_version: str


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
MODEL_PATH = os.environ.get("MODEL_PATH", "/models/predictor.pkl")
MIN_TRAIN_SAMPLES = int(os.environ.get("MIN_TRAIN_SAMPLES", "100"))
BOOTSTRAP_FLOOR_SECONDS = float(os.environ.get("BOOTSTRAP_FLOOR_SECONDS", str(4 * 3600)))


class ModelHolder:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.booster: Optional[lgb.Booster] = None
        self.feature_cols: List[str] = FEATURE_COLS
        self.user_stats: Dict[str, UserStats] = {}
        self.model_version: str = "bootstrap"
        self.n_train: int = 0
        self.trained_at: Optional[str] = None

    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            log.warning("no model file at %s — staying in bootstrap mode", path)
            return False
        with open(path, "rb") as fh:
            bundle = pickle.load(fh)
        with self.lock:
            self.booster = lgb.Booster(model_str=bundle["booster"])
            self.feature_cols = bundle["feature_cols"]
            self.user_stats = {
                u: UserStats(n=int(s["n"]), mean_log_rt=float(s["mean_log_rt"]))
                for u, s in bundle["user_stats"].items()
            }
            self.model_version = bundle["model_version"]
            self.n_train = int(bundle["train_metrics"]["n_train"])
            self.trained_at = bundle.get("trained_at")
        log.info("loaded model %s (n_train=%d)", self.model_version, self.n_train)
        return True

    def is_ready(self) -> bool:
        return self.booster is not None and self.n_train >= MIN_TRAIN_SAMPLES


HOLDER = ModelHolder()
HOLDER.load(MODEL_PATH)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
# Use a per-module registry rather than the global default so test
# re-imports (or multi-tenant deployments in the same process) don't
# collide on duplicate metric names.
METRICS_REGISTRY = CollectorRegistry()
PREDICT_LATENCY = Histogram(
    "runtime_predictor_predict_latency_seconds",
    "Per-request /predict latency",
    buckets=(0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
    registry=METRICS_REGISTRY,
)
PREDICT_COUNT = Counter(
    "runtime_predictor_predict_total",
    "Predictions served",
    ["mode"],  # "model" | "bootstrap"
    registry=METRICS_REGISTRY,
)
RETRAIN_COUNT = Counter(
    "runtime_predictor_retrain_total",
    "Retrains attempted",
    ["status"],  # "ok" | "error"
    registry=METRICS_REGISTRY,
)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="runtime-predictor", version="m5")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
def readyz() -> dict:
    return {
        "ok": True,
        "ready": HOLDER.is_ready(),
        "model_version": HOLDER.model_version,
        "n_train": HOLDER.n_train,
        "min_train_samples": MIN_TRAIN_SAMPLES,
    }


@app.get("/metrics")
def metrics():
    return Response(generate_latest(METRICS_REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    t0 = time.perf_counter()
    bootstrap = not HOLDER.is_ready()

    if bootstrap:
        ceiling = req.user_time_limit_seconds if req.user_time_limit_seconds else BOOTSTRAP_FLOOR_SECONDS
        pred_s = float(min(ceiling, BOOTSTRAP_FLOOR_SECONDS))
        PREDICT_COUNT.labels(mode="bootstrap").inc()
        latency = time.perf_counter() - t0
        PREDICT_LATENCY.observe(latency)
        return PredictResponse(
            pred_seconds=pred_s,
            pred_minutes=pred_s / 60.0,
            model_version="bootstrap",
            bootstrap=True,
            latency_ms=latency * 1000.0,
        )

    user_stats = HOLDER.user_stats.get(req.user)
    row = make_inference_row(req.model_dump(), user_stats)
    with HOLDER.lock:
        log_pred = float(HOLDER.booster.predict(row[HOLDER.feature_cols].to_numpy())[0])
    pred_s = float(np.expm1(max(0.0, log_pred)))
    # Clamp at 24 h — anything beyond that is the user's --time territory.
    pred_s = min(pred_s, 24 * 3600.0)
    PREDICT_COUNT.labels(mode="model").inc()
    latency = time.perf_counter() - t0
    PREDICT_LATENCY.observe(latency)
    return PredictResponse(
        pred_seconds=pred_s,
        pred_minutes=pred_s / 60.0,
        model_version=HOLDER.model_version,
        bootstrap=False,
        latency_ms=latency * 1000.0,
    )


@app.post("/retrain", response_model=RetrainResponse)
def retrain(req: RetrainRequest) -> RetrainResponse:
    from .train import train as train_fn

    out = req.output or MODEL_PATH
    try:
        metrics = train_fn(
            req.trace_path,
            out,
            holdout_frac=req.holdout_frac,
            n_estimators=req.n_estimators,
            rotate=req.rotate,
        )
    except Exception as e:
        RETRAIN_COUNT.labels(status="error").inc()
        log.exception("retrain failed")
        raise HTTPException(status_code=500, detail=f"retrain failed: {e}")
    HOLDER.load(out)
    RETRAIN_COUNT.labels(status="ok").inc()
    return RetrainResponse(ok=True, metrics=metrics, model_version=HOLDER.model_version)
