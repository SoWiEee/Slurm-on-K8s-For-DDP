"""Job trace loader.

Two formats supported:

1. **Normalized JSON** (what the simulator consumes natively) — list of
   dicts with keys ``job_id, user, gpu_count, gpu_type, submit_ts,
   runtime, mem_req, mps_req``. ``submit_ts`` and ``runtime`` are seconds
   (float). ``mps_req`` is the per-GPU MPS slot count in [1, MPS_PER_GPU];
   for whole-GPU jobs use ``MPS_PER_GPU`` (default 4).

2. **Philly raw** (``cluster_log_data.json`` from
   https://github.com/msr-fiddle/philly-traces) — auto-detected by the
   presence of ``status``/``vc``/``submitted_time`` fields. The loader
   normalises into format (1).

The Philly trace itself does **not** include MPS data (jobs are scheduled
in whole-GPU units). To exercise the M3 score factors we *augment* a
configurable fraction of single-GPU jobs by lowering ``mps_req`` to a
random tier {1, 2, 3, 4}. This is documented in §M4 of
``docs/scheduler.md`` (risk note).
"""
from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any, Iterable, List, Optional

MPS_PER_GPU = 4


@dataclass(frozen=True)
class Job:
    job_id: str
    user: str
    gpu_count: int
    gpu_type: str
    submit_ts: float
    runtime: float
    mem_req: float
    mps_req: int  # per-GPU MPS slots; MPS_PER_GPU = whole-GPU

    def as_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Normalized format
# ---------------------------------------------------------------------------
def load_normalized(path: str) -> List[Job]:
    with open(path, "r") as fh:
        raw = json.load(fh)
    return [_job_from_dict(item) for item in raw]


def _job_from_dict(item: dict) -> Job:
    return Job(
        job_id=str(item["job_id"]),
        user=str(item.get("user", "anon")),
        gpu_count=int(item["gpu_count"]),
        gpu_type=str(item.get("gpu_type", "rtx4070")),
        submit_ts=float(item["submit_ts"]),
        runtime=float(item["runtime"]),
        mem_req=float(item.get("mem_req", 0.0)),
        mps_req=int(item.get("mps_req", MPS_PER_GPU)),
    )


# ---------------------------------------------------------------------------
# Philly raw format
# ---------------------------------------------------------------------------
def _parse_philly_ts(s: Optional[str]) -> Optional[float]:
    if not s or s in ("None", "null"):
        return None
    # Philly uses "YYYY-MM-DD HH:MM:SS"
    try:
        return datetime.fromisoformat(s.replace("/", "-")).timestamp()
    except ValueError:
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            return None


def load_philly(
    path: str,
    *,
    max_jobs: Optional[int] = None,
    augment_mps_fraction: float = 0.30,
    seed: int = 42,
) -> List[Job]:
    """Load the upstream Philly ``cluster_log_data.json`` and normalise."""
    with open(path, "r") as fh:
        raw = json.load(fh)
    rng = random.Random(seed)
    jobs: List[Job] = []
    for entry in raw:
        submit = _parse_philly_ts(entry.get("submitted_time"))
        attempts = entry.get("attempts") or []
        if not submit or not attempts:
            continue
        # use the longest successful attempt as the runtime
        best = None
        for att in attempts:
            start = _parse_philly_ts(att.get("start_time"))
            end = _parse_philly_ts(att.get("end_time"))
            if start and end and end > start:
                rt = end - start
                if best is None or rt > best[0]:
                    best = (rt, att)
        if not best:
            continue
        runtime, att = best
        # gpu_count: detail field is a list of {ip, gpus:[...]}
        gpu_count = 0
        for d in att.get("detail", []) or []:
            gpu_count += len(d.get("gpus", []))
        if gpu_count <= 0:
            continue
        mps_req = MPS_PER_GPU
        if gpu_count == 1 and rng.random() < augment_mps_fraction:
            mps_req = rng.choice([1, 2, 3])  # not 4 == whole
        jobs.append(
            Job(
                job_id=str(entry.get("jobid", len(jobs))),
                user=str(entry.get("user", "anon")),
                gpu_count=gpu_count,
                gpu_type=str(entry.get("vc", "rtx4070")),
                submit_ts=submit,
                runtime=runtime,
                mem_req=0.0,
                mps_req=mps_req,
            )
        )
        if max_jobs and len(jobs) >= max_jobs:
            break
    return jobs


def load_auto(path: str, **kwargs: Any) -> List[Job]:
    """Heuristic: if the JSON top-level is a Philly-style array, normalise."""
    with open(path, "r") as fh:
        raw = json.load(fh)
    if isinstance(raw, list) and raw and "submitted_time" in raw[0]:
        return load_philly(path, **kwargs)
    return [_job_from_dict(item) for item in raw]


# ---------------------------------------------------------------------------
# Synthetic Philly-style subsample (no network needed)
# ---------------------------------------------------------------------------
def generate_philly_like(
    n_jobs: int = 1000,
    *,
    seed: int = 42,
    horizon_seconds: float = 5 * 24 * 3600,
    n_users: int = 40,
) -> List[Job]:
    """Synthetic trace with statistics close to published Philly subsamples.

    - GPU counts drawn from {1,2,4,8} with weights observed in Philly
      (~75% are 1-GPU, ~12% 2-GPU, ~10% 4-GPU, ~3% 8-GPU).
    - Runtimes are heavy-tailed log-normal (median ~30 min, p95 ~6h).
    - Submit timestamps Poisson-arrival, mean rate tuned so a 1k subsample
      spans ~5 days.
    - 30 % of single-GPU jobs are MPS-fractional ({1, 2, 3} slots).
    """
    rng = random.Random(seed)
    users = [f"u{i:02d}" for i in range(n_users)]
    gpu_choices = [1, 2, 4, 8]
    gpu_weights = [0.75, 0.12, 0.10, 0.03]
    gpu_types = ["rtx4070", "v100", "p100"]
    gpu_type_weights = [0.55, 0.30, 0.15]

    jobs: List[Job] = []
    t = 0.0
    mean_gap = horizon_seconds / max(1, n_jobs)
    for i in range(n_jobs):
        # Poisson arrival ⇒ exponential gap
        gap = rng.expovariate(1.0 / mean_gap) if mean_gap > 0 else 0.0
        t += gap
        gpu = rng.choices(gpu_choices, gpu_weights)[0]
        # log-normal runtime: median ~1800s, sigma 1.4 ⇒ heavy tail
        runtime = max(60.0, rng.lognormvariate(7.5, 1.4))
        mps = MPS_PER_GPU
        if gpu == 1 and rng.random() < 0.30:
            mps = rng.choice([1, 2, 3])
        jobs.append(
            Job(
                job_id=f"phl-{i:05d}",
                user=rng.choice(users),
                gpu_count=gpu,
                gpu_type=rng.choices(gpu_types, gpu_type_weights)[0],
                submit_ts=round(t, 3),
                runtime=round(runtime, 3),
                mem_req=0.0,
                mps_req=mps,
            )
        )
    return jobs


def generate_burst_heavy(
    n_jobs: int = 1000,
    *,
    seed: int = 42,
    horizon_seconds: float = 5 * 24 * 3600,
    n_users: int = 40,
    burst_period_seconds: float = 6 * 3600,
    burst_active_seconds: float = 2 * 3600,
    burst_concentration: float = 0.80,
) -> List[Job]:
    """Same job-size mix as `generate_philly_like` but with day-cycle bursts.

    A `burst_concentration` fraction of arrivals lands inside the
    `burst_active_seconds`-long active windows of each `burst_period_seconds`
    cycle (~33% of horizon). The rest is sprinkled across the gaps. This
    stresses the scheduler — peak queue length 3–4× the Poisson case —
    and is the most adverse environment for fragmentation reconcilers
    (head-of-line pile-ups + lots of small-MPS jobs queuing behind whole-
    GPU ones).
    """
    rng = random.Random(seed)
    users = [f"u{i:02d}" for i in range(n_users)]
    gpu_choices = [1, 2, 4, 8]
    gpu_weights = [0.75, 0.12, 0.10, 0.03]
    gpu_types = ["rtx4070", "v100", "p100"]
    gpu_type_weights = [0.55, 0.30, 0.15]

    n_burst = int(round(n_jobs * burst_concentration))
    n_gap = n_jobs - n_burst
    n_cycles = max(1, int(horizon_seconds / burst_period_seconds))

    # Build a deterministic list of submit_ts then attach job features.
    submits: List[float] = []
    for _ in range(n_burst):
        cyc = rng.randrange(n_cycles)
        offset = rng.uniform(0.0, burst_active_seconds)
        submits.append(cyc * burst_period_seconds + offset)
    for _ in range(n_gap):
        submits.append(rng.uniform(0.0, horizon_seconds))
    submits.sort()

    jobs: List[Job] = []
    for i, t in enumerate(submits):
        gpu = rng.choices(gpu_choices, gpu_weights)[0]
        runtime = max(60.0, rng.lognormvariate(7.5, 1.4))
        mps = MPS_PER_GPU
        if gpu == 1 and rng.random() < 0.30:
            mps = rng.choice([1, 2, 3])
        jobs.append(
            Job(
                job_id=f"burst-{i:05d}",
                user=rng.choice(users),
                gpu_count=gpu,
                gpu_type=rng.choices(gpu_types, gpu_type_weights)[0],
                submit_ts=round(t, 3),
                runtime=round(runtime, 3),
                mem_req=0.0,
                mps_req=mps,
            )
        )
    return jobs


def generate_ali_like(
    n_jobs: int = 1000,
    *,
    seed: int = 42,
    horizon_seconds: float = 5 * 24 * 3600,
    n_users: int = 60,
) -> List[Job]:
    """Approximate Alibaba PAI / ALI-Cluster MLaaS trace characteristics.

    Reference: Weng et al., NSDI'22 "MLaaS in the Wild" — heavily
    fractionalized, short tail, mostly single-GPU.

    - 90% single-GPU, 7% 2-GPU, 3% 4-GPU.
    - Runtimes shorter than Philly: median ~13 min, p95 ~3h (log-normal mu=6.8, sigma=1.2).
    - 60% of single-GPU jobs are MPS-fractional, often <= 0.5 GPU (mps {1,2}).
    - Diurnal arrival: rate doubles during the daytime 12h of each 24h cycle.
    """
    rng = random.Random(seed)
    users = [f"u{i:02d}" for i in range(n_users)]
    gpu_choices = [1, 2, 4]
    gpu_weights = [0.90, 0.07, 0.03]
    gpu_types = ["a10", "v100", "rtx4070"]
    gpu_type_weights = [0.60, 0.25, 0.15]

    # Diurnal arrival: thinning a uniform sample through a 1+sin envelope.
    raw_ts = [rng.uniform(0.0, horizon_seconds) for _ in range(n_jobs * 3)]
    def accept(t: float) -> bool:
        phase = (t % (24 * 3600)) / (24 * 3600)  # 0..1 over a day
        # daytime peak around 0.5 (noon), night trough around 0.0/1.0
        envelope = 0.5 + 0.5 * math.sin(2 * math.pi * (phase - 0.25))
        return rng.random() < envelope
    submits = sorted([t for t in raw_ts if accept(t)])[:n_jobs]
    # If sampling was too aggressive, top up uniformly.
    while len(submits) < n_jobs:
        submits.append(rng.uniform(0.0, horizon_seconds))
    submits.sort()

    jobs: List[Job] = []
    for i, t in enumerate(submits):
        gpu = rng.choices(gpu_choices, gpu_weights)[0]
        runtime = max(60.0, rng.lognormvariate(6.8, 1.2))  # median ~900s, p95 ~3h
        mps = MPS_PER_GPU
        if gpu == 1 and rng.random() < 0.60:
            mps = rng.choice([1, 2])
        jobs.append(
            Job(
                job_id=f"ali-{i:05d}",
                user=rng.choice(users),
                gpu_count=gpu,
                gpu_type=rng.choices(gpu_types, gpu_type_weights)[0],
                submit_ts=round(t, 3),
                runtime=round(runtime, 3),
                mem_req=0.0,
                mps_req=mps,
            )
        )
    return jobs


TRACE_FAMILIES = {
    "philly": generate_philly_like,
    "burst": generate_burst_heavy,
    "ali": generate_ali_like,
}


def generate_by_family(family: str, **kwargs) -> List[Job]:
    if family not in TRACE_FAMILIES:
        raise ValueError(f"unknown trace family {family!r}; "
                         f"choices: {sorted(TRACE_FAMILIES)}")
    return TRACE_FAMILIES[family](**kwargs)


def write_normalized(jobs: Iterable[Job], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        json.dump([j.as_dict() for j in jobs], fh, indent=2)
