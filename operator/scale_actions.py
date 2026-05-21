"""Scale action executors — scale_up / scale_down / keep.

Each method here is dispatched from ReconcilerMixin._process_pool based
on the policy's decision. Side effects:
  - scale_up:   patch StatefulSet replicas, cancel any in-flight drains,
                record OTel scale_up_decision span, write cooldown annotation
  - scale_down: drain affected nodes first, only patch when idle (or after
                drain_timeout force-kill), record drain metrics
  - keep:       just log + emit "skipped" metric with reason

Mixin pattern: reads ``self.client``, ``self.actuator``, ``self.cfg``,
``self.last_scale_up_at``, ``self._provisioning``, ``self._draining_*``,
``self.logger`` from OperatorApp. Split out of operator/app.py in v5
review C2.
"""
from __future__ import annotations

import time

from metrics import (
    _CHECKPOINT_GUARD_BLOCKS_TOTAL,
    _DRAIN_TIMEOUT_TOTAL,
    _DRAIN_TOTAL,
    _SCALE_DOWN_TOTAL,
    _SCALE_SKIPPED_TOTAL,
    _SCALE_UP_TOTAL,
)
from models import PartitionConfig

import otel as _otel

_COOLDOWN_ANNOTATION = "slurm.k8s/last-scale-up-at"


class ScaleActionsMixin:
    """scale_up / scale_down / keep actuators dispatched from _process_pool."""

    def _do_scale_up(self, partition_cfg: PartitionConfig, state, decision, key: str, now: float) -> None:
        # If nodes were draining for a previous scale-down, cancel so they can accept jobs again.
        draining = self._draining_nodes.pop(key, set())
        self._draining_started.pop(key, None)
        for node_name in draining:
            try:
                self.client.resume_slurm_node(node_name)
            except Exception:  # noqa: BLE001
                pass
        self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
        self.last_scale_up_at[key] = now
        # Emit scale_up_decision span and store context for the subsequent
        # k8s_provisioning span (which is emitted when pods become ready).
        _prov_ctx = None
        if _otel.enabled():
            with _otel.start_span(
                "scale_up_decision",
                attributes={
                    "pool": key,
                    "partition": partition_cfg.partition,
                    "from_replicas": state.current_replicas,
                    "to_replicas": decision.target_replicas,
                    "reason": decision.reason,
                    "pending_jobs": state.pending_jobs,
                },
            ) as _span:
                from opentelemetry import context as _octx
                _prov_ctx = _octx.get_current()
        self._provisioning[key] = (now, decision.target_replicas, _prov_ctx)
        try:
            self.client.set_annotation(
                "statefulset", partition_cfg.worker_statefulset,
                _COOLDOWN_ANNOTATION, str(now),
            )
        except Exception:  # noqa: BLE001
            pass  # best-effort; in-memory value is still correct for this cycle
        _SCALE_UP_TOTAL.labels(pool=key).inc()
        self.logger.emit(
            "scale_action",
            policy=self.cfg.policy_name,
            partition=partition_cfg.partition,
            action="scale_up",
            statefulset=partition_cfg.worker_statefulset,
            from_replicas=state.current_replicas,
            to_replicas=decision.target_replicas,
            reason=decision.reason,
            pending_jobs=state.pending_jobs,
            running_jobs=state.running_jobs,
            busy_nodes=state.busy_nodes,
        )

    def _do_scale_down(self, partition_cfg: PartitionConfig, state, decision, key: str,
                       cooldown_elapsed: float, cooldown_remaining: int) -> None:
        if cooldown_elapsed < partition_cfg.scale_down_cooldown:
            _SCALE_SKIPPED_TOTAL.labels(pool=key, reason="cooldown").inc()
            self.logger.emit(
                "scale_skipped",
                policy=self.cfg.policy_name,
                partition=partition_cfg.partition,
                action="scale_down",
                statefulset=partition_cfg.worker_statefulset,
                from_replicas=state.current_replicas,
                to_replicas=decision.target_replicas,
                reason="scale_down_cooldown",
                cooldown_remaining_seconds=cooldown_remaining,
                pending_jobs=state.pending_jobs,
                running_jobs=state.running_jobs,
                busy_nodes=state.busy_nodes,
            )
            return

        # Drain-then-scale: mark the nodes being removed as DRAIN so no new jobs
        # land on them, then wait for running jobs to finish before patching replicas.
        nodes_to_drain = {
            f"{partition_cfg.worker_statefulset}-{i}"
            for i in range(decision.target_replicas, state.current_replicas)
        }
        draining = self._draining_nodes.get(key, set())
        started = self._draining_started.setdefault(key, {})
        new_nodes = nodes_to_drain - draining
        now_ts = time.time()
        for node_name in new_nodes:
            try:
                self.client.drain_slurm_node(node_name)
            except Exception:  # noqa: BLE001
                pass
            started[node_name] = now_ts
        if new_nodes:
            draining = draining | new_nodes
            self._draining_nodes[key] = draining
            _DRAIN_TOTAL.labels(pool=key).inc()
            self.logger.emit(
                "drain_initiated",
                policy=self.cfg.policy_name,
                partition=partition_cfg.partition,
                statefulset=partition_cfg.worker_statefulset,
                draining_nodes=sorted(new_nodes),
                target_replicas=decision.target_replicas,
            )

        # R1: force-kill any node whose drain has exceeded drain_timeout_seconds.
        # Otherwise a hung srun step keeps cpu_alloc != 0 forever and the pool
        # never shrinks.
        timeout = partition_cfg.drain_timeout_seconds
        force_killed: set[str] = set()
        if timeout > 0:
            for node_name in list(draining):
                drain_started = started.get(node_name)
                if drain_started is None:
                    continue
                if self.client.get_node_cpu_alloc(node_name) == 0:
                    continue
                age = now_ts - drain_started
                if age <= timeout:
                    continue
                try:
                    self.client.cancel_jobs_on_node(node_name)
                    self.client.down_slurm_node(node_name, reason="drain-timeout")
                except Exception:  # noqa: BLE001
                    pass
                force_killed.add(node_name)
                _DRAIN_TIMEOUT_TOTAL.labels(pool=key, node=node_name).inc()
                self.logger.emit(
                    "drain_timeout_force_kill",
                    level="WARN",
                    policy=self.cfg.policy_name,
                    partition=partition_cfg.partition,
                    statefulset=partition_cfg.worker_statefulset,
                    node=node_name,
                    drained_for_seconds=int(age),
                    drain_timeout_seconds=timeout,
                )

        all_idle = all(
            n in force_killed or self.client.get_node_cpu_alloc(n) == 0
            for n in draining
        )
        if all_idle:
            self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
            self._draining_nodes.pop(key, None)
            self._draining_started.pop(key, None)
            _SCALE_DOWN_TOTAL.labels(pool=key).inc()
            self.logger.emit(
                "scale_action",
                policy=self.cfg.policy_name,
                partition=partition_cfg.partition,
                action="scale_down",
                statefulset=partition_cfg.worker_statefulset,
                from_replicas=state.current_replicas,
                to_replicas=decision.target_replicas,
                reason=decision.reason,
                pending_jobs=state.pending_jobs,
                running_jobs=state.running_jobs,
                busy_nodes=state.busy_nodes,
            )
        else:
            _SCALE_SKIPPED_TOTAL.labels(pool=key, reason="draining").inc()
            self.logger.emit(
                "scale_skipped",
                policy=self.cfg.policy_name,
                partition=partition_cfg.partition,
                action="scale_down",
                statefulset=partition_cfg.worker_statefulset,
                from_replicas=state.current_replicas,
                to_replicas=decision.target_replicas,
                reason="waiting_for_drain",
                draining_nodes=sorted(draining),
                pending_jobs=state.pending_jobs,
                running_jobs=state.running_jobs,
                busy_nodes=state.busy_nodes,
            )

    def _do_keep(self, partition_cfg: PartitionConfig, state, decision, key: str,
                 checkpoint_age: int | None) -> None:
        _guard_reasons = (
            "checkpoint_unknown_block_scale_down",
            "checkpoint_stale_block_scale_down",
        )
        if decision.reason in _guard_reasons:
            _CHECKPOINT_GUARD_BLOCKS_TOTAL.labels(pool=key).inc()
            _SCALE_SKIPPED_TOTAL.labels(pool=key, reason="checkpoint_guard").inc()
        else:
            _SCALE_SKIPPED_TOTAL.labels(pool=key, reason="no_action").inc()
        self.logger.emit(
            "scale_skipped",
            policy=self.cfg.policy_name,
            partition=partition_cfg.partition,
            action="keep",
            statefulset=partition_cfg.worker_statefulset,
            from_replicas=state.current_replicas,
            to_replicas=decision.target_replicas,
            reason=decision.reason,
            checkpoint_age_seconds=checkpoint_age,
            pending_jobs=state.pending_jobs,
            running_jobs=state.running_jobs,
            busy_nodes=state.busy_nodes,
        )
