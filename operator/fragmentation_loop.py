"""Fragmentation reconcile loop (Phase 6 M7).

Pulls JobView/NodeView from slurmrestd, feeds them to
FragmentationDetector + RequeueDecider, and optionally requeues via
``scontrol requeue`` (depending on ``shadowMode``). Runs in a dedicated
daemon thread so a slow scontrol invocation can't delay the main
scaling reconcile.

Mixin pattern: reads ``self.rest``, ``self.partition_cfgs``,
``self._fragmentation_*`` state, ``self.logger`` from OperatorApp.
Split out of operator/app.py in v5 review C2.
"""
from __future__ import annotations

import time
from typing import Any

from fragmentation import (
    jobs_from_slurm_rest,
    nodes_from_slurm_rest,
)
from metrics import (
    _FRAGMENTATION_BLOCKED_JOBS,
    _FRAGMENTATION_SCORE,
    _REQUEUE_TOTAL,
    _REQUEUE_VICTIMS,
)


class FragmentationLoopMixin:
    """Periodic detect → decide → (optional) requeue cycle."""

    def _fragmentation_loop(self) -> None:
        """Runs at ``FRAGMENTATION_MIN_INTERVAL_SECONDS`` cadence.

        The decider's own rate limiter is what prevents bursty requeues —
        this loop is just the scheduler. Errors are logged but never
        crash the thread (the operator's circuit breaker only covers the
        scaling reconcile path).
        """
        assert self._fragmentation_reconciler is not None
        while True:
            time.sleep(max(1.0, self._fragmentation_interval))
            try:
                self._fragmentation_tick()
            except Exception as exc:  # noqa: BLE001
                self.logger.emit(
                    "fragmentation_error", level="WARN",
                    error=type(exc).__name__, message=str(exc),
                )

    def _fragmentation_tick(self) -> None:
        """Single detect → decide → (optional) actuate cycle."""
        if self.rest is None or self._fragmentation_reconciler is None:
            return
        partitions = self._fragmentation_partitions or tuple(
            p.partition for p in self.partition_cfgs
        )
        # Pull a single union of jobs across the partitions we care about.
        rest_jobs: list[dict] = []
        for part in partitions:
            try:
                rest_jobs.extend(self.rest.list_jobs(part))
            except Exception as exc:  # noqa: BLE001
                self.logger.emit(
                    "fragmentation_error", level="WARN",
                    error=f"list_jobs({part}):{type(exc).__name__}", message=str(exc),
                )
                return
        try:
            rest_nodes = self.rest.list_nodes()
        except Exception as exc:  # noqa: BLE001
            self.logger.emit(
                "fragmentation_error", level="WARN",
                error=f"list_nodes:{type(exc).__name__}", message=str(exc),
            )
            return

        mps_per_node = self._fragmentation_reconciler.detector.mps_per_node
        jobs = jobs_from_slurm_rest(rest_jobs, mps_per_node=mps_per_node)
        nodes = nodes_from_slurm_rest(rest_nodes, mps_per_node=mps_per_node)
        result = self._fragmentation_reconciler.reconcile(jobs, nodes, now=time.time())

        _FRAGMENTATION_SCORE.set(result.snapshot.score)
        _FRAGMENTATION_BLOCKED_JOBS.set(len(result.snapshot.pending_blocked))

        # Log + count by reason bucket. The decider's ``reason`` is one of
        # "no-fragmentation" | "rate-limited:..." | "no-candidates-..." |
        # "<unblock detail>"; bucket them so Prometheus cardinality stays
        # bounded.
        reason_label = (
            "rate-limited" if result.reason.startswith("rate-limited")
            else "no-fragmentation" if result.reason == "no-fragmentation"
            else "no-victims" if result.reason.startswith("no-")
            else "unblock"
        )
        _REQUEUE_TOTAL.labels(reason=reason_label).inc()

        log_fields: dict[str, Any] = {
            "score": round(result.snapshot.score, 4),
            "blocked": [j.job_id for j in result.snapshot.pending_blocked[:5]],
            "reason": result.reason,
            "shadow": result.shadow,
        }
        if result.decision is not None:
            log_fields["target_jobs"] = list(result.decision.target_job_ids)
            log_fields["unblocks"] = list(result.decision.blocked_job_ids)
            log_fields["requeued_jobs"] = list(result.requeued)
            if result.actuator_errors:
                log_fields["actuator_errors"] = list(result.actuator_errors)
            for _ in result.requeued:
                _REQUEUE_VICTIMS.inc()
            self.logger.emit("requeue_decision", **log_fields)
        else:
            # Quiet by default — only log non-trivial reasons. A
            # "no-fragmentation" tick every 60s would flood slurm log.
            if reason_label != "no-fragmentation":
                self.logger.emit("requeue_skipped", **log_fields)
