"""M9 / M10-F weight-tuner — UCB1 outer-loop weight adapter.

FastAPI on :8003.
  GET  /healthz     liveness probe
  GET  /weights     current best (α, δ, ε) arm for score function
  POST /feedback    supply mean-JCT reward for a completed arm window
  GET  /stats       per-arm pull counts + mean rewards

Background task: every COLLECTOR_INTERVAL_S seconds, query slurmrestd for
completed jobs, compute −mean_JCT_hours, and auto-update the bandit.

Arm format: (alpha, delta, epsilon) 3-float tuple following sim_env.default_arm_grid.
Beta (vramFit) is fixed at BETA_FIXED — not tuned by the bandit.

Run:
    python -m services.weight_tuner.serve --port 8003
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Sibling imports (work whether invoked as module or from repo root)
# ---------------------------------------------------------------------------
import sys
_REPO = Path(__file__).parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from services.weight_tuner.bandit import UCB1Policy  # noqa: E402
from services.weight_tuner.sim_env import default_arm_grid  # noqa: E402

# ---------------------------------------------------------------------------
# Config from env
# ---------------------------------------------------------------------------
PORT                  = int(os.getenv("PORT", "8003"))
STATE_DIR             = Path(os.getenv("STATE_DIR", "/tmp/wt-state"))
SLURMRESTD_URL        = os.getenv("SLURMRESTD_URL",
                                   "http://slurm-restapi.slurm.svc:6820")
SLURM_API_VERSION     = os.getenv("SLURM_API_VERSION", "v0.0.41")
SLURM_USER            = os.getenv("SLURM_USER", "root")
COLLECTOR_INTERVAL_S  = float(os.getenv("COLLECTOR_INTERVAL_S", "300"))
MIN_JOBS_FOR_UPDATE   = int(os.getenv("MIN_JOBS_FOR_UPDATE", "5"))
BETA_FIXED            = float(os.getenv("BETA_FIXED", "0.20"))
UCB1_C                = float(os.getenv("UCB1_C", "1.0"))
LOG_LEVEL             = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s [wt] %(levelname)s %(message)s")
log = logging.getLogger("weight_tuner")

# ---------------------------------------------------------------------------
# Bandit state
# ---------------------------------------------------------------------------
ARMS = default_arm_grid()          # list of (alpha, delta, epsilon) tuples
_policy: UCB1Policy = UCB1Policy(arms=ARMS, c=UCB1_C)
_state_file = STATE_DIR / "ucb1_state.json"
_last_poll_ts: float = 0.0         # Unix epoch of last slurmrestd poll
_current_arm: tuple = ARMS[0]      # arm that was active during last poll window


def _save_state() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "t": _policy._t,
        "arms": [
            {"arm": list(a), "n": _policy._n[a], "mean": _policy._mean[a]}
            for a in ARMS
        ],
    }
    _state_file.write_text(json.dumps(data))


def _load_state() -> None:
    global _policy
    if not _state_file.exists():
        return
    try:
        data = json.loads(_state_file.read_text())
        _policy._t = data.get("t", 0)
        for entry in data.get("arms", []):
            arm = tuple(entry["arm"])
            if arm in _policy._n:
                _policy._n[arm] = entry["n"]
                _policy._mean[arm] = entry["mean"]
        log.info("restored bandit state from %s (t=%d)", _state_file, _policy._t)
    except Exception as exc:
        log.warning("could not restore state (%s) — starting fresh", exc)


# ---------------------------------------------------------------------------
# slurmrestd helpers
# ---------------------------------------------------------------------------

def _slurm_get(path: str, timeout: int = 8) -> dict:
    url = f"{SLURMRESTD_URL}{path}"
    req = urllib.request.Request(url)
    req.add_header("X-SLURM-USER-NAME", SLURM_USER)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _fetch_completed_jobs_since(since_ts: float) -> list[dict]:
    """Return COMPLETED jobs whose end_time > since_ts."""
    try:
        data = _slurm_get(f"/slurm/{SLURM_API_VERSION}/jobs")
    except Exception as exc:
        log.debug("slurmrestd unreachable: %s", exc)
        return []
    jobs = []
    for j in data.get("jobs", []):
        state = j.get("job_state", "")
        if isinstance(state, list):
            state = state[0] if state else ""
        if state.upper() != "COMPLETED":
            continue
        end_t = j.get("end_time", 0)
        if isinstance(end_t, dict):
            end_t = end_t.get("number", 0)
        if float(end_t) > since_ts:
            jobs.append(j)
    return jobs


def _compute_jct_hours(jobs: list[dict]) -> Optional[float]:
    jcts = []
    for j in jobs:
        submit = j.get("submit_time", 0)
        end    = j.get("end_time",    0)
        if isinstance(submit, dict):
            submit = submit.get("number", 0)
        if isinstance(end, dict):
            end = end.get("number", 0)
        submit, end = float(submit), float(end)
        if submit > 0 and end > submit:
            jcts.append((end - submit) / 3600.0)
    return float(np.mean(jcts)) if jcts else None


# ---------------------------------------------------------------------------
# Background collector
# ---------------------------------------------------------------------------

async def _collector_loop() -> None:
    global _last_poll_ts, _current_arm
    await asyncio.sleep(COLLECTOR_INTERVAL_S)        # initial warm-up delay
    while True:
        try:
            since = _last_poll_ts
            now   = time.time()
            jobs  = _fetch_completed_jobs_since(since)
            if len(jobs) >= MIN_JOBS_FOR_UPDATE:
                jct_h = _compute_jct_hours(jobs)
                if jct_h is not None and jct_h > 0:
                    reward = -jct_h          # bandit maximises → negate JCT
                    arm    = _current_arm
                    _policy.update(arm, [], reward)
                    log.info("auto-feedback arm=%s n_jobs=%d mean_jct_h=%.3f reward=%.4f",
                             arm, len(jobs), jct_h, reward)
                    _save_state()
            _last_poll_ts = now
            # Select new arm for next window
            _current_arm = _policy.select([])
            log.debug("next arm selected: %s (total_t=%d)", _current_arm, _policy._t)
        except Exception as exc:
            log.warning("collector error: %s", exc)
        await asyncio.sleep(COLLECTOR_INTERVAL_S)


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load_state()
    global _current_arm
    _current_arm = _policy.select([])
    asyncio.create_task(_collector_loop())
    log.info("weight-tuner started; initial arm=%s", _current_arm)
    yield


app = FastAPI(title="weight-tuner", version="1.0.0", lifespan=_lifespan)


class WeightsResponse(BaseModel):
    arm:     List[float]   # [alpha, delta, epsilon]
    alpha:   float
    delta:   float
    epsilon: float
    beta:    float = BETA_FIXED
    n_pulls: int
    total_t: int
    policy:  str = "ucb1"


class FeedbackRequest(BaseModel):
    arm:     List[float]   # [alpha, delta, epsilon]
    reward:  float         # caller supplies −JCT_hours (negative is fine too)
    context: Optional[List[float]] = None


class StatsEntry(BaseModel):
    arm:   List[float]
    n:     int
    mean:  float


@app.get("/healthz")
async def healthz():
    return {"ok": True, "total_t": _policy._t}


@app.get("/weights", response_model=WeightsResponse)
async def get_weights():
    arm = _current_arm
    a, d, e = float(arm[0]), float(arm[1]), float(arm[2])
    return WeightsResponse(
        arm=[a, d, e],
        alpha=a, delta=d, epsilon=e,
        n_pulls=int(_policy._n.get(arm, 0)),
        total_t=int(_policy._t),
    )


@app.post("/feedback", status_code=204)
async def post_feedback(req: FeedbackRequest):
    arm = tuple(round(float(v), 8) for v in req.arm)
    # Snap to nearest known arm (float comparison safety)
    best_match = min(ARMS, key=lambda a: sum((x - y) ** 2
                                             for x, y in zip(a, arm)))
    _policy.update(best_match, req.context or [], req.reward)
    _save_state()
    log.info("feedback arm=%s reward=%.4f total_t=%d",
             best_match, req.reward, _policy._t)


@app.get("/stats", response_model=List[StatsEntry])
async def get_stats():
    return [
        StatsEntry(arm=list(a), n=_policy._n[a], mean=_policy._mean[a])
        for a in ARMS
    ]


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import argparse
    import uvicorn

    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--host", default="0.0.0.0")
    args = p.parse_args()
    uvicorn.run("services.weight_tuner.serve:app",
                host=args.host, port=args.port, log_level=LOG_LEVEL.lower())
