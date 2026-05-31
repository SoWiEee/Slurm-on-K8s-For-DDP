"""Phase 6 M3 score, applied as a priority kicker on top of multifactor.

The Lua plugin in ``chart/templates/configmap-job-submit.yaml`` evaluates
the same factors at sbatch time. Here we reimplement them in Python so
the offline simulator can run without slurmctld. Coefficients and tier
list match the chart defaults — keep them in sync when tuning::

    chart/values.yaml: slurm.jobSubmit.scoreWeights.{mpsFit, vramFit,
                       fragmentation}
    chart/values.yaml: slurm.jobSubmit.vramTiers
"""
from __future__ import annotations

import statistics
import re
from dataclasses import dataclass
from typing import List, Optional

from ..cluster import Cluster
from ..loader import Job, MPS_PER_GPU
from .multifactor import MultifactorScheduler


_DEFAULT_TIERS_GB = (12, 24)


def _gpu_type_to_vram(gpu_type: str) -> Optional[int]:
    return {
        "rtx4070": 12,
        "v100": 16,
        "p100": 16,
        "a10": 24,
        "h100": 80,
    }.get(gpu_type.lower())


def clamp01(x: float) -> float:
    if x != x:
        return 0.0
    return max(0.0, min(1.0, x))


def parse_submit_mps_req(tres_per_node: str | None) -> int:
    """Parse the live Lua job_submit mps:N / mps=N convention."""
    if not tres_per_node:
        return 0
    match = re.search(r"mps[:=](\d+)", tres_per_node)
    return int(match.group(1)) if match else 0


def parse_submit_vram_req(features: str | None) -> int:
    """Parse the live Lua job_submit vram-Ng feature convention."""
    if not features:
        return 0
    match = re.search(r"vram-(\d+)g", features)
    return int(match.group(1)) if match else 0


@dataclass(frozen=True)
class SubmitScoreFactors:
    score: float
    mps_fit: float
    vram_fit: float
    topology: float
    fragmentation: float
    pred_runtime: float


def submit_score_factors(
    *,
    tres_per_node: str | None = None,
    features: str | None = None,
    mps_per_node: int = 100,
    vram_tiers=_DEFAULT_TIERS_GB,
    alpha: float = 0.40,
    beta: float = 0.20,
    gamma: float = 0.00,
    delta: float = 0.20,
    epsilon: float = 0.00,
    pred_runtime: float = 0.5,
) -> SubmitScoreFactors:
    """Reference implementation for the live Lua submit-time score.

    The production Lua plugin cannot see full placement state during submit,
    so this intentionally mirrors its stateless proxy instead of
    ScoreScheduler's cluster-aware simulator factors.
    """
    mps_req = parse_submit_mps_req(tres_per_node)
    vram_req = parse_submit_vram_req(features)
    tiers = sorted(vram_tiers)
    vram_max = max(tiers)

    if mps_req <= 0:
        mps_fit = 1.0
    elif mps_req > mps_per_node:
        mps_fit = 0.0
    else:
        mps_fit = clamp01(mps_req / mps_per_node)

    if vram_req == 0:
        vram_fit = 0.5
    else:
        fit_tier = next((tier for tier in tiers if tier >= vram_req), None)
        vram_fit = 0.0 if fit_tier is None else clamp01(1 - (fit_tier - vram_req) / vram_max)

    if mps_req <= 0 or mps_req >= mps_per_node:
        fragmentation = 0.0
    else:
        x = mps_req / mps_per_node
        fragmentation = clamp01(4 * x * (1 - x))

    topology = 0.5
    score = clamp01(
        alpha * mps_fit
        + beta * vram_fit
        + gamma * topology
        - delta * fragmentation
        + epsilon * pred_runtime
    )
    return SubmitScoreFactors(
        score=score,
        mps_fit=mps_fit,
        vram_fit=vram_fit,
        topology=topology,
        fragmentation=fragmentation,
        pred_runtime=pred_runtime,
    )


class ScoreScheduler(MultifactorScheduler):
    name = "score"
    backfill = True

    def __init__(
        self,
        alpha: float = 0.40,    # mps_fit
        beta: float = 0.20,     # vram_fit
        delta: float = 0.20,    # fragmentation (subtractive)
        epsilon: float = 0.30,  # f_runtime_short (SJF kicker); 0 = disabled
        score_gain: float = 1000.0,
        vram_tiers=_DEFAULT_TIERS_GB,
        mps_per_node: int = MPS_PER_GPU,  # per-GPU slot count
        runtime_horizon: float = 3600.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.alpha = alpha
        self.beta = beta
        self.delta = delta
        self.epsilon = epsilon
        self.score_gain = score_gain
        self.vram_tiers = sorted(vram_tiers)
        self.mps_per_node = mps_per_node
        self.runtime_horizon = runtime_horizon

    # ---- factor implementations ------------------------------------
    def f_mps_fit(self, j: Job, cluster: Cluster) -> float:
        """Bin-packing fit score: how tightly the job fills its best GPU slot.

        score = mps_req / best_gpu_free_mps, where best_gpu is the one with
        the smallest free_mps >= mps_req (first-fit-decreasing spirit).

        1.0 = perfect fit (exactly fills the available slot).
        Rewards small jobs on nearly-full GPUs as much as large jobs on full
        GPUs — enables MPS colocation instead of monopolising with large jobs.
        Returns 0.0 if the job cannot fit anywhere.
        """
        best_fit = None
        for node in cluster.nodes:
            for gpu in node.gpus:
                if gpu.free_mps >= j.mps_req:
                    fit = j.mps_req / gpu.free_mps   # 1.0 = tight, 0 = loose
                    if best_fit is None or fit > best_fit:
                        best_fit = fit
        return best_fit if best_fit is not None else 0.0

    def f_vram_fit(self, j: Job) -> float:
        v = _gpu_type_to_vram(j.gpu_type)
        if v is None:
            return 0.5
        fit_tier = next((t for t in self.vram_tiers if t >= v), None)
        if fit_tier is None:
            return 0.0
        denom = max(self.vram_tiers)
        return max(0.0, 1.0 - (fit_tier - v) / denom)

    def f_fragmentation(self, j: Job, cluster: Cluster) -> float:
        # Penalise placements that would leave awkward MPS residuals.
        # For single-node clusters: use per-GPU free residual after placement.
        free = cluster.free_mps_per_node()
        if not free:
            return 0.0
        if len(free) == 1:
            # Single-node: measure GPU-level fragmentation (residual after fit)
            node = cluster.nodes[0]
            residuals = []
            for gpu in node.gpus:
                if gpu.free_mps >= j.mps_req:
                    residuals.append(gpu.free_mps - j.mps_req)
            if not residuals:
                return 1.0  # doesn't fit → max fragmentation penalty
            best_residual = min(residuals)
            return best_residual / cluster.mps_per_gpu  # 0 = perfect, 1 = worst
        # Heuristic: jobs at ~50% of a node's slots are worst (parabolic in
        # the lua plugin); multi-GPU jobs that span nodes evenly are best.
        if j.gpu_count >= 1 and j.mps_req >= self.mps_per_node:
            return 0.0
        x = j.mps_req / self.mps_per_node
        local = 4 * x * (1 - x)
        # cluster-level skew: stddev of free_mps normalised by total
        spread = statistics.pstdev(free) / max(1.0, statistics.fmean(free))
        return min(1.0, 0.5 * local + 0.5 * spread)

    def f_runtime_short(self, j: Job) -> float:
        # SJF kicker fed by the M5 predictor. Sim assumes a perfect predictor
        # (uses j.runtime as the prediction); E4 ablation only flips epsilon.
        # 1.0 for an instant job, decays to ~0 for a job >> runtime_horizon.
        if j.runtime <= 0 or self.runtime_horizon <= 0:
            return 1.0
        return self.runtime_horizon / (self.runtime_horizon + j.runtime)

    def score(self, j: Job, cluster: Cluster) -> float:
        s = (
            self.alpha * self.f_mps_fit(j, cluster)
            + self.beta * self.f_vram_fit(j)
            - self.delta * self.f_fragmentation(j, cluster)
            + self.epsilon * self.f_runtime_short(j)
        )
        return max(0.0, min(1.0, s))

    # ---- ordering ---------------------------------------------------
    def order(self, pending: List[Job], cluster: Cluster, now: float) -> List[Job]:
        max_gpu = max((j.gpu_count for j in pending), default=1)

        def key(j: Job):
            base = self._priority(j, now, max_gpu)
            kicker = self.score_gain * self.score(j, cluster)
            return (-(base + kicker), j.submit_ts, j.job_id)

        return sorted(pending, key=key)
