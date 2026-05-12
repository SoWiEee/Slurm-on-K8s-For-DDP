"""Sim-backed pull function for the M9 weight-tuner bandits.

The bandit wants a `pull(arm, context) -> reward` callable. An arm is
a tuple of score weights (alpha, beta, delta, epsilon); context is a
3-vector summarising the trace. We turn that into one `sim.runner.run`
call and return -mean_JCT_hours as the reward (higher = better).

Caching: the same (arm, context_key) tuple should always yield the
same reward (sim is deterministic given the trace). We cache so the
bandit can revisit arms without paying the simulation cost twice.
The context key is the (trace_family, seed) pair the caller stashes
in `_context_index` — we keep them aligned by index.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

# Make `import sim.*` work whether we're invoked from repo root or not.
_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from sim.loader import generate_by_family  # noqa: E402
from sim.runner import run as sim_run  # noqa: E402


Arm = Tuple[float, ...]


@dataclass
class TraceSpec:
    family: str
    seed: int
    n_jobs: int = 1000


def context_vector(spec: TraceSpec) -> Tuple[float, float, float]:
    """3-dim context: normalised job count + mean mps + mean gpu_count.

    Built from the synthetic generator's outputs, so we can cheaply
    score the context without running the simulator. All features land
    roughly in [0, 1] for the trace families we ship.
    """
    jobs = generate_by_family(spec.family, n_jobs=spec.n_jobs, seed=spec.seed)
    n = len(jobs)
    mean_mps = sum(j.mps_req for j in jobs) / max(n, 1) / 4.0  # MPS_PER_GPU
    mean_gpu = sum(j.gpu_count for j in jobs) / max(n, 1) / 8.0
    return (n / 2000.0, mean_mps, mean_gpu)


class SimPull:
    """Callable `(arm, context) -> reward` that runs the simulator.

    Context tuples are matched back to trace specs by exact tuple lookup
    in `_lookup`. Callers register specs up front via `register_trace()`.
    """

    def __init__(
        self,
        *,
        nodes: int = 4,
        gpus_per_node: int = 4,
        scheduler: str = "score",
        beta: float = 0.20,            # fixed; tuner explores alpha/delta/epsilon
        fragmentation: bool = False,
    ):
        self.nodes = nodes
        self.gpus_per_node = gpus_per_node
        self.scheduler = scheduler
        self.beta = beta
        self.fragmentation = fragmentation
        self._lookup: Dict[Tuple[float, ...], TraceSpec] = {}
        self._cache: Dict[Tuple[Arm, Tuple[float, ...]], float] = {}

    def register_trace(self, spec: TraceSpec) -> Tuple[float, ...]:
        ctx = context_vector(spec)
        self._lookup[ctx] = spec
        return ctx

    def __call__(self, arm: Arm, context: Sequence[float]) -> float:
        ctx_key = tuple(context)
        cache_key = (tuple(arm), ctx_key)
        if cache_key in self._cache:
            return self._cache[cache_key]
        spec = self._lookup.get(ctx_key)
        if spec is None:
            raise KeyError(f"unknown context {ctx_key} — register the trace first")
        alpha, delta, epsilon = arm
        jobs = generate_by_family(spec.family, n_jobs=spec.n_jobs, seed=spec.seed)
        metrics, _ = sim_run(
            jobs,
            n_nodes=self.nodes,
            gpus_per_node=self.gpus_per_node,
            scheduler_name=self.scheduler,
            scheduler_kwargs={
                "alpha": alpha, "beta": self.beta,
                "delta": delta, "epsilon": epsilon,
            },
            fragmentation=self.fragmentation,
        )
        jct_mean = metrics.summary()["jct_mean"]
        reward = -jct_mean / 3600.0  # negative hours; bandit maximises
        self._cache[cache_key] = reward
        return reward


def default_arm_grid() -> List[Arm]:
    """3×3×3 = 27 arms over (alpha, delta, epsilon).

    Picked to span the M8 sensitivity grid plus an extra epsilon axis
    that M8's 5×5 didn't sweep. Beta stays at 0.20 (M8 default).
    """
    arms: List[Arm] = []
    for a in (0.10, 0.40, 0.70):
        for d in (0.05, 0.20, 0.40):
            for e in (0.00, 0.30, 0.60):
                arms.append((a, d, e))
    return arms
