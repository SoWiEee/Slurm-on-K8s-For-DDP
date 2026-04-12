"""Operator application — main poll loop.

OperatorApp wires together all components and runs the control loop:
  collect state → evaluate policy → actuate → sleep → repeat.

Also contains JsonLogger (structured log emitter) and StatefulSetActuator
(thin wrapper around K8sClient.patch_replicas kept for separation of concerns).
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from prometheus_client import start_http_server

from collector import ClusterStateCollector, PartitionConfigLoader
from k8s import K8sClient
from metrics import (
    _CHECKPOINT_GUARD_BLOCKS_TOTAL,
    _CIRCUIT_BREAKER_ERRORS,
    _CURRENT_REPLICAS,
    _DRAIN_TOTAL,
    _PODS_READY,
    _POLL_DURATION,
    _PROVISIONING_LATENCY,
    _SCALE_DOWN_TOTAL,
    _SCALE_SKIPPED_TOTAL,
    _SCALE_UP_TOTAL,
)
from models import Config, PartitionConfig
from policy import CheckpointAwareQueuePolicy
from slurm import SlurmRestClient

_COOLDOWN_ANNOTATION = "slurm.k8s/last-scale-up-at"


class JsonLogger:
    def emit(self, event_type: str, level: str = "INFO", **fields: Any) -> None:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event_type": event_type,
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)


class StatefulSetActuator:
    def __init__(self, client: K8sClient):
        self.client = client

    def patch_replicas(self, statefulset: str, replicas: int) -> None:
        self.client.patch_replicas(statefulset, replicas)


class OperatorApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.logger = JsonLogger()
        self.client = K8sClient(cfg)
        self.partition_cfgs = PartitionConfigLoader.load(cfg)
        self.rest = (
            SlurmRestClient(
                cfg.slurm_rest_url,
                api_version=cfg.slurm_rest_api_version,
                jwt_key_path=cfg.slurm_jwt_key_path,
            )
            if cfg.slurm_rest_url else None
        )
        self.collector = ClusterStateCollector(self.client, self.partition_cfgs, self.rest)
        self.policy = CheckpointAwareQueuePolicy(cfg.checkpoint_guard_enabled)
        self.actuator = StatefulSetActuator(self.client)
        # Restore cooldown timestamps from StatefulSet annotations so a pod
        # restart does not reset the cooldown clock and cause an immediate
        # scale-down that was previously guarded against.
        self.last_scale_up_at: dict[str, float] = {}
        # Track pending provisioning: pool → (scale_up_timestamp, target_replicas).
        # Cleared once readyReplicas reaches the target; used to emit _PROVISIONING_LATENCY.
        self._provisioning: dict[str, tuple[float, int]] = {}
        # Drain-then-scale: pool → set of node names that have been drained and are
        # waiting for running jobs to finish before replicas are patched down.
        self._draining_nodes: dict[str, set[str]] = {}
        # Checkpoint missing-since tracking: pool → timestamp when file was first not found.
        # Used to implement grace period before blocking scale-down on missing checkpoint.
        self._checkpoint_missing_since: dict[str, float] = {}
        # Circuit-breaker state: tracks consecutive loop-level errors for exponential backoff.
        self._consecutive_errors: int = 0
        for _p in self.partition_cfgs:
            _raw: str | None = None
            try:
                _raw = self.client.get_annotation(
                    "statefulset", _p.worker_statefulset, _COOLDOWN_ANNOTATION
                )
            except Exception:  # noqa: BLE001
                pass
            try:
                self.last_scale_up_at[_p.worker_statefulset] = float(_raw) if _raw else 0.0
            except ValueError:
                self.last_scale_up_at[_p.worker_statefulset] = 0.0

    def run(self) -> None:
        rest_available = self.rest is not None and self.rest.ping()
        self.logger.emit(
            "startup",
            policy=self.cfg.policy_name,
            query_mode="rest" if rest_available else "exec",
            rest_url=self.cfg.slurm_rest_url or None,
            config=asdict(self.cfg),
            partitions=[asdict(p) for p in self.partition_cfgs],
        )
        if self.rest is not None and not rest_available:
            self.logger.emit(
                "error", level="WARN",
                message="SLURM_REST_URL is set but slurmrestd ping failed; falling back to kubectl exec",
                rest_url=self.cfg.slurm_rest_url,
            )
            self.rest = None
            self.collector._rest = None
        # Warn if checkpoint guard is enabled but no path is configured — the guard
        # will be silently skipped for those pools at runtime.
        if self.cfg.checkpoint_guard_enabled:
            for _p in self.partition_cfgs:
                if not _p.checkpoint_path:
                    self.logger.emit(
                        "error", level="WARN",
                        message=(
                            f"checkpoint guard enabled but checkpoint_path is empty for pool "
                            f"'{_p.worker_statefulset}' — guard is effectively disabled for this pool; "
                            f"set CHECKPOINT_PATH or per-pool checkpoint_path to activate"
                        ),
                        pool=_p.worker_statefulset,
                    )
        start_http_server(8000)
        while True:
            _loop_start = time.time()
            try:
                all_states = self.collector.collect_all_partition_states()
            except Exception as exc:  # noqa: BLE001  — circuit breaker
                self._consecutive_errors += 1
                _sleep = min(2.0 ** min(self._consecutive_errors, 6), 60.0)
                _CIRCUIT_BREAKER_ERRORS.set(self._consecutive_errors)
                self.logger.emit(
                    "error", level="ERROR",
                    message=f"collect_all_partition_states failed (consecutive={self._consecutive_errors}): {exc}",
                    consecutive_errors=self._consecutive_errors,
                    backoff_seconds=_sleep,
                )
                pathlib.Path("/tmp/operator-alive").touch()
                time.sleep(_sleep)
                continue
            if self._consecutive_errors > 0:
                self.logger.emit(
                    "circuit_closed", level="INFO",
                    message="operator loop recovered",
                    previous_consecutive_errors=self._consecutive_errors,
                )
                self._consecutive_errors = 0
                _CIRCUIT_BREAKER_ERRORS.set(0)
            for partition_cfg in self.partition_cfgs:
                self._process_pool(partition_cfg, all_states)
            _POLL_DURATION.observe(time.time() - _loop_start)
            pathlib.Path("/tmp/operator-alive").touch()
            pathlib.Path("/tmp/operator-ready").touch()
            time.sleep(self.cfg.poll_interval)

    def _process_pool(self, partition_cfg: PartitionConfig, all_states: dict) -> None:
        key = partition_cfg.worker_statefulset
        try:
            state = all_states[key]
            _CURRENT_REPLICAS.labels(pool=key).set(state.current_replicas)

            # Pods-ready gauge + provisioning latency tracking
            ready = self.client.get_ready_replicas(key)
            _PODS_READY.labels(pool=key).set(ready)
            if key in self._provisioning:
                prov_start, prov_target = self._provisioning[key]
                if ready >= prov_target:
                    _PROVISIONING_LATENCY.labels(pool=key).observe(time.time() - prov_start)
                    del self._provisioning[key]

            checkpoint_age = self.collector.get_checkpoint_age_seconds(partition_cfg.checkpoint_path)

            # Track when the checkpoint file was first seen as missing so the
            # grace period in evaluate() can allow early scale-downs.
            if partition_cfg.checkpoint_path and checkpoint_age is None:
                self._checkpoint_missing_since.setdefault(key, time.time())
            else:
                self._checkpoint_missing_since.pop(key, None)
            _missing_first = self._checkpoint_missing_since.get(key)
            _missing_since = (time.time() - _missing_first) if _missing_first is not None else None

            decision = self.policy.evaluate(partition_cfg, state, checkpoint_age, _missing_since)

            now = time.time()
            cooldown_elapsed = now - self.last_scale_up_at[key]
            cooldown_remaining = max(partition_cfg.scale_down_cooldown - int(cooldown_elapsed), 0)

            self.logger.emit(
                "loop_observation",
                policy=self.cfg.policy_name,
                partition=partition_cfg.partition,
                state=asdict(state),
                decision=asdict(decision),
                checkpoint_age_seconds=checkpoint_age,
                cooldown_remaining_seconds=cooldown_remaining,
            )

            if decision.action == "scale_up":
                self._do_scale_up(partition_cfg, state, decision, key, now)
            elif decision.action == "scale_down":
                self._do_scale_down(partition_cfg, state, decision, key, cooldown_elapsed, cooldown_remaining)
            else:
                self._do_keep(partition_cfg, state, decision, key, checkpoint_age)
        except Exception as exc:  # noqa: BLE001
            self.logger.emit("error", level="ERROR", partition=partition_cfg.partition, statefulset=key, message=str(exc))

    def _do_scale_up(self, partition_cfg: PartitionConfig, state, decision, key: str, now: float) -> None:
        # If nodes were draining for a previous scale-down, cancel so they can accept jobs again.
        draining = self._draining_nodes.pop(key, set())
        for node_name in draining:
            try:
                self.client.resume_slurm_node(node_name)
            except Exception:  # noqa: BLE001
                pass
        self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
        self.last_scale_up_at[key] = now
        self._provisioning[key] = (now, decision.target_replicas)
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
        new_nodes = nodes_to_drain - draining
        for node_name in new_nodes:
            try:
                self.client.drain_slurm_node(node_name)
            except Exception:  # noqa: BLE001
                pass
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

        all_idle = all(self.client.get_node_cpu_alloc(n) == 0 for n in draining)
        if all_idle:
            self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
            self._draining_nodes.pop(key, None)
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
