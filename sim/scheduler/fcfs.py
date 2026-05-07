"""FCFS — strict submit-order, head-of-line blocking."""
from __future__ import annotations

from typing import List

from ..cluster import Cluster
from ..loader import Job


class FCFSScheduler:
    name = "fcfs"
    backfill = False

    def order(self, pending: List[Job], cluster: Cluster, now: float) -> List[Job]:
        return sorted(pending, key=lambda j: (j.submit_ts, j.job_id))
