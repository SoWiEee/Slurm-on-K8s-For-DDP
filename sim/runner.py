"""Discrete-event runner: drive a trace through a Cluster + Scheduler.

Event types are minimal — ``submit`` (job appears in the pending queue)
and ``end`` (running job releases its allocation). After every event the
runner re-orders pending jobs via the scheduler, then walks the ordered
list trying to allocate. With ``scheduler.backfill=True`` we skip jobs
that don't fit; otherwise we stop at the first head-of-line block (FCFS).

Usage::

    python -m sim.runner --trace philly.json --scheduler score \\
                         --nodes 4 --gpus-per-node 4 \\
                         --output out/score.csv
"""
from __future__ import annotations

import argparse
import heapq
import json
import os
import sys
import time
from typing import List, Tuple

from .cluster import Cluster
from .loader import (
    Job, MPS_PER_GPU, TRACE_FAMILIES,
    generate_by_family, load_auto, write_normalized,
)
from .metrics import MetricCollector
from .scheduler import make as make_scheduler


def run(
    jobs: List[Job],
    *,
    n_nodes: int,
    gpus_per_node: int,
    scheduler_name: str,
    mps_per_gpu: int = MPS_PER_GPU,
    scheduler_kwargs=None,
) -> Tuple[MetricCollector, Cluster]:
    """Run the trace through the simulator."""
    cluster = Cluster(n_nodes=n_nodes, gpus_per_node=gpus_per_node, mps_per_gpu=mps_per_gpu)
    scheduler = make_scheduler(scheduler_name, **(scheduler_kwargs or {}))
    metrics = MetricCollector()

    pending: List[Job] = []
    by_id = {j.job_id: j for j in jobs}
    for j in jobs:
        metrics.record_submit(
            job_id=j.job_id, user=j.user, gpu_count=j.gpu_count,
            mps_req=j.mps_req, submit_ts=j.submit_ts, runtime=j.runtime)

    events: List = []
    seq = 0
    for j in sorted(jobs, key=lambda x: x.submit_ts):
        heapq.heappush(events, (j.submit_ts, seq, "submit", j.job_id))
        seq += 1

    now = 0.0
    metrics.sample_util(0.0, 0.0)

    def try_dispatch():
        nonlocal seq
        ordered = scheduler.order(pending, cluster, now)
        for j in list(ordered):
            if cluster.try_allocate(j) is not None:
                pending.remove(j)
                metrics.record_start(j.job_id, now)
                heapq.heappush(events, (now + j.runtime, seq, "end", j.job_id))
                seq += 1
            elif not scheduler.backfill:
                break

    while events:
        t, _s, kind, payload = heapq.heappop(events)
        now = t
        if kind == "submit":
            pending.append(by_id[payload])
        elif kind == "end":
            cluster.release(payload)
            metrics.record_end(payload, now)
        try_dispatch()
        metrics.sample_util(now, cluster.utilization())

    metrics.sample_util(now, cluster.utilization())
    return metrics, cluster


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sim.runner")
    p.add_argument("--trace", help="path to JSON trace (normalized or Philly)")
    p.add_argument("--synth-jobs", type=int, default=0,
                   help="generate synthetic jobs (family selected by --trace-family)")
    p.add_argument("--synth-seed", type=int, default=42)
    p.add_argument("--trace-family", choices=sorted(TRACE_FAMILIES), default="philly")
    p.add_argument("--scheduler", choices=["fcfs", "multifactor", "score"], default="fcfs")
    p.add_argument("--nodes", type=int, default=4)
    p.add_argument("--gpus-per-node", type=int, default=4)
    p.add_argument("--mps-per-gpu", type=int, default=MPS_PER_GPU)
    p.add_argument("--output", help="write per-job CSV here")
    p.add_argument("--write-trace", help="write the loaded/synthetic trace as normalized JSON")
    p.add_argument("--summary-json", help="write summary dict as JSON to this path")
    p.add_argument("--alpha", type=float, default=None)
    p.add_argument("--beta", type=float, default=None)
    p.add_argument("--delta", type=float, default=None)
    p.add_argument("--epsilon", type=float, default=None)
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.synth_jobs > 0:
        jobs = generate_by_family(args.trace_family,
                                  n_jobs=args.synth_jobs, seed=args.synth_seed)
    elif args.trace:
        jobs = load_auto(args.trace)
    else:
        print("error: --trace or --synth-jobs is required", file=sys.stderr)
        return 2

    if args.write_trace:
        write_normalized(jobs, args.write_trace)

    sched_kwargs = {k: getattr(args, k) for k in ("alpha", "beta", "delta", "epsilon")
                    if getattr(args, k) is not None}

    t0 = time.monotonic()
    metrics, _cluster = run(
        jobs,
        n_nodes=args.nodes,
        gpus_per_node=args.gpus_per_node,
        scheduler_name=args.scheduler,
        mps_per_gpu=args.mps_per_gpu,
        scheduler_kwargs=sched_kwargs or None,
    )
    wall = time.monotonic() - t0

    summary = metrics.summary()
    summary.update({
        "scheduler": args.scheduler,
        "wall_seconds": round(wall, 3),
        "nodes": args.nodes,
        "gpus_per_node": args.gpus_per_node,
        "synth_seed": args.synth_seed if args.synth_jobs > 0 else None,
        "trace_family": args.trace_family if args.synth_jobs > 0 else "loaded",
        **sched_kwargs,
    })

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        metrics.write_per_job_csv(args.output)
    if args.summary_json:
        os.makedirs(os.path.dirname(args.summary_json) or ".", exist_ok=True)
        with open(args.summary_json, "w") as fh:
            json.dump(summary, fh, indent=2)

    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
