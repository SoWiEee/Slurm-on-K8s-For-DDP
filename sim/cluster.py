"""Cluster model with MPS-aware GPU slots.

A ``Cluster`` is a list of nodes. Each node has ``gpus_per_node`` GPUs and
each GPU exposes ``mps_per_gpu`` slots. Jobs allocate either:

- whole GPUs (``mps_req == mps_per_gpu``), spread across nodes if needed; or
- a single GPU's MPS fraction (``gpu_count == 1`` + ``mps_req < mps_per_gpu``).

The simulator is intentionally simple — no preemption, no fragmentation
heuristics. Schedulers call :py:meth:`Cluster.try_allocate`; it returns a
list of ``Allocation`` records or ``None``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .loader import Job, MPS_PER_GPU


@dataclass
class Allocation:
    job_id: str
    node_id: int
    gpu_indices: List[int]  # which GPUs on that node
    mps_per_gpu: int        # slots reserved on each listed GPU


@dataclass
class _GPU:
    free_mps: int


@dataclass
class _Node:
    node_id: int
    gpus: List[_GPU]

    def free_whole_gpus(self, mps_per_gpu: int) -> List[int]:
        return [i for i, g in enumerate(self.gpus) if g.free_mps == mps_per_gpu]

    def free_mps_total(self) -> int:
        return sum(g.free_mps for g in self.gpus)


@dataclass
class Cluster:
    n_nodes: int
    gpus_per_node: int
    mps_per_gpu: int = MPS_PER_GPU
    nodes: List[_Node] = field(init=False)
    active: dict = field(init=False)  # job_id -> List[Allocation]

    def __post_init__(self) -> None:
        self.nodes = [
            _Node(i, [_GPU(self.mps_per_gpu) for _ in range(self.gpus_per_node)])
            for i in range(self.n_nodes)
        ]
        self.active = {}

    # ----- introspection -----
    def total_gpus(self) -> int:
        return self.n_nodes * self.gpus_per_node

    def total_mps(self) -> int:
        return self.total_gpus() * self.mps_per_gpu

    def used_mps(self) -> int:
        return self.total_mps() - sum(n.free_mps_total() for n in self.nodes)

    def utilization(self) -> float:
        return self.used_mps() / self.total_mps() if self.total_mps() else 0.0

    def free_mps_per_node(self) -> List[int]:
        return [n.free_mps_total() for n in self.nodes]

    # ----- allocation -----
    def can_allocate(self, job: Job) -> bool:
        return self._plan(job) is not None

    def try_allocate(self, job: Job) -> Optional[List[Allocation]]:
        plan = self._plan(job)
        if plan is None:
            return None
        for alloc in plan:
            node = self.nodes[alloc.node_id]
            for gi in alloc.gpu_indices:
                node.gpus[gi].free_mps -= alloc.mps_per_gpu
        self.active[job.job_id] = plan
        return plan

    def release(self, job_id: str) -> None:
        plan = self.active.pop(job_id, None)
        if plan is None:
            return
        for alloc in plan:
            node = self.nodes[alloc.node_id]
            for gi in alloc.gpu_indices:
                node.gpus[gi].free_mps += alloc.mps_per_gpu

    # ----- planner -----
    def _plan(self, job: Job) -> Optional[List[Allocation]]:
        # Single-GPU fractional MPS request
        if job.gpu_count == 1 and job.mps_req < self.mps_per_gpu:
            for node in self.nodes:
                # First-fit: pick the GPU with the smallest matching residual
                best = None
                for gi, g in enumerate(node.gpus):
                    if g.free_mps >= job.mps_req:
                        residual = g.free_mps - job.mps_req
                        if best is None or residual < best[1]:
                            best = (gi, residual)
                if best is not None:
                    return [Allocation(job.job_id, node.node_id, [best[0]], job.mps_req)]
            return None

        # Whole-GPU job — may span nodes
        needed = job.gpu_count
        plan: List[Allocation] = []
        # prefer fewer nodes (best-fit by free whole-GPUs)
        ranked = sorted(
            range(self.n_nodes),
            key=lambda i: -len(self.nodes[i].free_whole_gpus(self.mps_per_gpu)),
        )
        for ni in ranked:
            if needed <= 0:
                break
            free_gpus = self.nodes[ni].free_whole_gpus(self.mps_per_gpu)
            if not free_gpus:
                continue
            take = free_gpus[: min(needed, len(free_gpus))]
            plan.append(Allocation(job.job_id, ni, take, self.mps_per_gpu))
            needed -= len(take)
        if needed > 0:
            return None
        return plan
