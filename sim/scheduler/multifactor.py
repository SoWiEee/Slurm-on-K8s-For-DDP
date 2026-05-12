"""Slurm-multifactor priority approximation.

Reproduces the spirit of Slurm's ``priority/multifactor`` plugin without
the full fairshare tree. We compute::

    priority(J) = w_age * age_norm
                + w_jobsize * jobsize_norm
                + w_qos * qos_norm

where every ``*_norm`` is in [0, 1]. Defaults mirror chart values
(``slurm.scheduling.priorityWeights``) so the simulator and the live
deployment stay roughly aligned.
"""
from __future__ import annotations

from typing import List

from ..cluster import Cluster
from ..loader import Job


_DEFAULT_AGE_REF = 7200.0  # 2 hours wait → age_norm = 1.0


class MultifactorScheduler:
    name = "multifactor"
    backfill = True  # FIFO with backfill, like sched/backfill

    def __init__(
        self,
        w_age: float = 1000.0,
        w_jobsize: float = 500.0,
        w_qos: float = 2000.0,
        age_ref: float = _DEFAULT_AGE_REF,
    ) -> None:
        self.w_age = w_age
        self.w_jobsize = w_jobsize
        self.w_qos = w_qos
        self.age_ref = age_ref

    def _priority(self, j: Job, now: float, max_gpu: int) -> float:
        age_norm = min(1.0, max(0.0, (now - j.submit_ts) / self.age_ref))
        jobsize_norm = j.gpu_count / max_gpu if max_gpu else 0.0
        qos_norm = 0.0  # no QoS classes in synthetic trace
        return (
            self.w_age * age_norm
            + self.w_jobsize * jobsize_norm
            + self.w_qos * qos_norm
        )

    def order(self, pending: List[Job], cluster: Cluster, now: float) -> List[Job]:
        max_gpu = max((j.gpu_count for j in pending), default=1)
        return sorted(
            pending,
            key=lambda j: (-self._priority(j, now, max_gpu), j.submit_ts, j.job_id),
        )
