"""Pluggable schedulers consumed by ``sim.runner``.

Each scheduler exposes a single method::

    def order(self, pending: List[Job], cluster: Cluster, now: float) -> List[Job]

returning ``pending`` re-ordered most-preferred-first. The runner then
walks the list, attempting allocation for each job in order; the first
job that does not fit blocks (FCFS / multifactor) or gets skipped over
(``score`` with ``backfill=True``).
"""
from .fcfs import FCFSScheduler
from .multifactor import MultifactorScheduler
from .score import ScoreScheduler

REGISTRY = {
    "fcfs": FCFSScheduler,
    "multifactor": MultifactorScheduler,
    "score": ScoreScheduler,
}


def make(name: str, **kwargs):
    if name not in REGISTRY:
        raise ValueError(f"unknown scheduler {name!r}; choose from {list(REGISTRY)}")
    return REGISTRY[name](**kwargs)
