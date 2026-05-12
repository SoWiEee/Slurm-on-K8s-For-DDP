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


class ScoreScheduler(MultifactorScheduler):
    name = "score"
    backfill = True

    def __init__(
        self,
        alpha: float = 0.40,    # mps_fit
        beta: float = 0.20,     # vram_fit
        delta: float = 0.20,    # fragmentation (subtractive)
        epsilon: float = 0.0,   # f_runtime_short (M5 predictor); 0 = predictor disabled
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
    def f_mps_fit(self, j: Job) -> float:
        if j.mps_req <= 0 or j.mps_req >= self.mps_per_node:
            return 1.0
        return j.mps_req / self.mps_per_node

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
        # Penalise placements that would skew per-node free MPS.
        free = cluster.free_mps_per_node()
        if not free or len(free) == 1:
            return 0.0
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
            self.alpha * self.f_mps_fit(j)
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
