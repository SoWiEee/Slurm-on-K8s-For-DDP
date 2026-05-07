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

The CLI prints a one-line summary in JSON for shell consumption.
"""
from __future__ import annotations

import argparse
import dataclasses
import heapq
import json
import os
import sys
import time
from typing import List, Tuple

from .cluster import Cluster
from .loader import Job, MPS_PER_GPU, generate_philly_like, load_auto, write_normalized
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
    fragmentation: bool = False,
    fragmentation_priority_gap: float = 100.0,
) -> Tuple[MetricCollector, Cluster]:
    """Run the trace through the simulator.

    ``fragmentation=True`` mirrors the M7 Gandiva-lite reconciler: after
    every event, if the head pending job cannot fit but the union of
    free + (a low-priority running job's slots) would let it fit, the
    runner requeues that running job (releases its slots, cancels its
    end-event, re-submits it at ``now``). The victim must be lower
    priority than the blocked head by ``fragmentation_priority_gap``.
    """
    cluster = Cluster(n_nodes=n_nodes, gpus_per_node=gpus_per_node, mps_per_gpu=mps_per_gpu)
    scheduler = make_scheduler(scheduler_name, **(scheduler_kwargs or {}))
    metrics = MetricCollector()
    requeue_count = 0

    pending: List[Job] = []
    by_id = {j.job_id: j for j in jobs}
    for j in jobs:
        metrics.record_submit(
            job_id=j.job_id, user=j.user, gpu_count=j.gpu_count,
            mps_req=j.mps_req, submit_ts=j.submit_ts, runtime=j.runtime)

    # Event queue: heap of (time, seq, kind, payload)
    events: List = []
    seq = 0
    for j in sorted(jobs, key=lambda x: x.submit_ts):
        heapq.heappush(events, (j.submit_ts, seq, "submit", j.job_id))
        seq += 1

    now = 0.0
    metrics.sample_util(0.0, 0.0)

    # job_id -> (priority_at_start, runtime, original_submit_ts, end_seq)
    running_meta: dict = {}
    requeue_attempts: dict = {}   # job_id -> total times kicked
    MAX_REQUEUES_PER_JOB = 2      # bound to keep sim O(N), avoids ping-pong

    def try_dispatch():
        nonlocal seq
        ordered = scheduler.order(pending, cluster, now)
        # Walk in priority order; break at first non-fit unless backfill enabled
        for j in list(ordered):
            if cluster.try_allocate(j) is not None:
                pending.remove(j)
                metrics.record_start(j.job_id, now)
                end_ts = now + j.runtime
                heapq.heappush(events, (end_ts, seq, "end", j.job_id))
                # remember per-job priority so the fragmentation reconciler
                # can compare victims fairly (it can't re-call _priority on
                # MultifactorScheduler without max_gpu context)
                max_gpu = max((x.gpu_count for x in pending + [j]), default=1)
                prio = (scheduler._priority(j, now, max_gpu)
                        if hasattr(scheduler, "_priority") else 0.0)
                running_meta[j.job_id] = (prio, j.runtime, j.submit_ts, seq)
                seq += 1
            elif not scheduler.backfill:
                break

    def try_fragmentation_reconcile():
        """Mirror operator/fragmentation.py at runner level.

        If the highest-priority pending job is blocked but freeing one or
        more lower-priority running jobs would let it fit, do that. We
        re-evaluate after every event; rate-limit is handled by the
        scheduler's own priority bookkeeping (a victim that just got
        kicked has the freshest submit_ts and won't be the head).
        """
        nonlocal requeue_count, seq
        if not fragmentation or not pending:
            return
        ordered = scheduler.order(pending, cluster, now)
        head = ordered[0]
        if cluster.can_allocate(head):
            return
        max_gpu = max((x.gpu_count for x in pending), default=1)
        head_prio = (scheduler._priority(head, now, max_gpu)
                     if hasattr(scheduler, "_priority") else 0.0)

        # Sort current running jobs by victim priority ascending — kill the
        # cheapest-to-evict first. Stop as soon as `head` becomes fit-able.
        victims = sorted(running_meta.items(), key=lambda kv: kv[1][0])
        for jid, (vprio, vrun, vsub, _eseq) in victims:
            if head_prio - vprio < fragmentation_priority_gap:
                break
            if requeue_attempts.get(jid, 0) >= MAX_REQUEUES_PER_JOB:
                continue
            cluster.release(jid)
            running_meta.pop(jid, None)
            # cancel the corresponding end-event by lazy-deletion: we just
            # rewrite the heap by rebuilding without that job_id's end.
            for i, (_t, _s, kind, payload) in enumerate(events):
                if kind == "end" and payload == jid:
                    events[i] = (events[i][0], events[i][1], "_cancelled", payload)
                    break
            # re-submit victim at `now` (loses any progress — Gandiva-lite
            # assumes resume from checkpoint, so this is the conservative
            # upper-bound on cost)
            old = by_id[jid]
            vj = dataclasses.replace(old, submit_ts=now)
            by_id[jid] = vj
            metrics.records[jid].submit_ts = now
            metrics.records[jid].start_ts = None
            metrics.records[jid].end_ts = None
            pending.append(vj)
            requeue_attempts[jid] = requeue_attempts.get(jid, 0) + 1
            requeue_count += 1
            if cluster.can_allocate(head):
                return

    while events:
        t, _s, kind, payload = heapq.heappop(events)
        now = t
        if kind == "submit":
            pending.append(by_id[payload])
        elif kind == "end":
            cluster.release(payload)
            running_meta.pop(payload, None)
            metrics.record_end(payload, now)
        elif kind == "_cancelled":
            pass  # tombstone for a requeued victim's old end-event
        # After any state change, re-attempt dispatch.
        try_dispatch()
        try_fragmentation_reconcile()
        try_dispatch()
        metrics.sample_util(now, cluster.utilization())

    metrics.sample_util(now, cluster.utilization())
    metrics.requeue_count = requeue_count
    return metrics, cluster


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sim.runner")
    p.add_argument("--trace", help="path to JSON trace (normalized or Philly)")
    p.add_argument("--synth-jobs", type=int, default=0,
                   help="generate this many synthetic Philly-like jobs instead")
    p.add_argument("--synth-seed", type=int, default=42)
    p.add_argument("--scheduler", choices=["fcfs", "multifactor", "score"],
                   default="fcfs")
    p.add_argument("--nodes", type=int, default=4)
    p.add_argument("--gpus-per-node", type=int, default=4)
    p.add_argument("--mps-per-gpu", type=int, default=MPS_PER_GPU)
    p.add_argument("--output", help="write per-job CSV here")
    p.add_argument("--write-trace", help="write the loaded/synthetic trace as normalized JSON")
    p.add_argument("--summary-json", help="write summary dict as JSON to this path")
    p.add_argument("--fragmentation", action="store_true",
                   help="enable M7-style fragmentation requeue reconciler")
    p.add_argument("--alpha", type=float, default=None)
    p.add_argument("--beta", type=float, default=None)
    p.add_argument("--delta", type=float, default=None)
    p.add_argument("--epsilon", type=float, default=None,
                   help="score scheduler weight for f_runtime_short (M5 predictor)")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.synth_jobs > 0:
        jobs = generate_philly_like(n_jobs=args.synth_jobs, seed=args.synth_seed)
    elif args.trace:
        jobs = load_auto(args.trace)
    else:
        print("error: --trace or --synth-jobs is required", file=sys.stderr)
        return 2

    if args.write_trace:
        write_normalized(jobs, args.write_trace)

    sched_kwargs = {}
    for k in ("alpha", "beta", "delta", "epsilon"):
        v = getattr(args, k)
        if v is not None:
            sched_kwargs[k] = v

    t0 = time.monotonic()
    metrics, _cluster = run(
        jobs,
        n_nodes=args.nodes,
        gpus_per_node=args.gpus_per_node,
        scheduler_name=args.scheduler,
        mps_per_gpu=args.mps_per_gpu,
        scheduler_kwargs=sched_kwargs or None,
        fragmentation=args.fragmentation,
    )
    wall = time.monotonic() - t0

    summary = metrics.summary()
    summary["scheduler"] = args.scheduler
    summary["wall_seconds"] = round(wall, 3)
    summary["nodes"] = args.nodes
    summary["gpus_per_node"] = args.gpus_per_node
    summary["fragmentation"] = args.fragmentation
    summary["requeue_count"] = getattr(metrics, "requeue_count", 0)
    summary.update({k: v for k, v in sched_kwargs.items()})

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
