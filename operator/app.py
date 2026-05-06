"""Operator application — event-driven reconcile loop (R21).

OperatorApp wires together all components and runs three watcher threads
plus a single reconcile consumer:

  K8s StatefulSet watch ──┐
  K8s Pod watch ──────────┼──► dedup queue ──► reconcile(pool) ──► actuate
  Slurm 1s state diff ────┤                       ▲
  60s timer (safety net) ─┘                       │
                                                  │
                            measure event_lag_seconds (event_ts → reconcile_start)

The previous polling-only loop was rebuilt around a `_PoolEventQueue` so
scale events trigger within sub-second of being observed by the K8s API
or by squeue, while a 60s timer-driven reconcile runs as a safety net so
a missed watch event cannot leave a pool stuck out of sync.

Also contains JsonLogger (structured log emitter) and StatefulSetActuator
(thin wrapper around K8sClient.patch_replicas kept for separation of concerns).
"""

from __future__ import annotations

import json
import pathlib
import queue
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from kubernetes import watch as k8s_watch
from prometheus_client import start_http_server

from collector import ClusterStateCollector, PartitionConfigLoader
from k8s import K8sClient
from metrics import (
    _CHECKPOINT_GUARD_BLOCKS_TOTAL,
    _CIRCUIT_BREAKER_ERRORS,
    _CURRENT_REPLICAS,
    _DRAIN_TIMEOUT_TOTAL,
    _DRAIN_TOTAL,
    _EVENT_LAG_SECONDS,
    _PODS_READY,
    _POLL_DURATION,
    _PROVISIONING_LATENCY,
    _QUEUE_DEDUP_DROPS,
    _RECONCILES_TOTAL,
    _SCALE_DOWN_TOTAL,
    _SCALE_SKIPPED_TOTAL,
    _SCALE_UP_TOTAL,
)
from models import Config, PartitionConfig, PartitionState
from policy import CheckpointAwareQueuePolicy
from slurm import SlurmRestClient

_COOLDOWN_ANNOTATION = "slurm.k8s/last-scale-up-at"
_TIMER_SOURCE = "timer"


class _PoolEventQueue:
    """Pool-keyed work queue with dedup.

    Multiple watchers (K8s STS, K8s Pod, Slurm diff, periodic timer) can
    enqueue the same pool key simultaneously; the consumer only needs to
    reconcile each pool once per "burst" so we collapse pending entries.

    `put` returns False (and increments a metric) if the pool already has
    an entry sitting in the queue — the existing entry will be consumed
    soon enough.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[str, str, float]] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()

    def put(self, pool_key: str, source: str) -> bool:
        with self._lock:
            if pool_key in self._pending:
                _QUEUE_DEDUP_DROPS.labels(pool=pool_key, source=source).inc()
                return False
            self._pending.add(pool_key)
        self._q.put((pool_key, source, time.time()))
        return True

    def get(self, timeout: float | None) -> tuple[str, str, float] | None:
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        with self._lock:
            self._pending.discard(item[0])
        return item


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
        # R1: pool → {node_name → epoch when it first entered DRAIN}.  Used to
        # force-kill jobs once drain_timeout_seconds elapses so a hung srun step
        # cannot pin the pool at max_replicas indefinitely.
        self._draining_started: dict[str, dict[str, float]] = {}
        # Checkpoint missing-since tracking: pool → timestamp when file was first not found.
        # Used to implement grace period before blocking scale-down on missing checkpoint.
        self._checkpoint_missing_since: dict[str, float] = {}
        # Circuit-breaker state: tracks consecutive loop-level errors for exponential backoff.
        self._consecutive_errors: int = 0
        # R21: event-driven plumbing.
        self._event_queue = _PoolEventQueue()
        self._cfg_by_key: dict[str, PartitionConfig] = {p.worker_statefulset: p for p in self.partition_cfgs}
        self._slurm_state_cache: dict[str, PartitionState] = {}
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
        # R21: spawn watcher threads. They are daemon threads so a clean
        # shutdown of the consumer loop tears them down implicitly.
        for thread_target, name in (
            (self._watch_statefulsets, "k8s-sts-watch"),
            (self._watch_pods, "k8s-pod-watch"),
            (self._poll_slurm_state, "slurm-diff"),
            (self._periodic_timer, "periodic-timer"),
        ):
            t = threading.Thread(target=thread_target, name=name, daemon=True)
            t.start()
        # Prime the queue so the first reconcile happens immediately, before
        # any watcher fires. Without this the readiness probe waits up to
        # `slurm_poll_interval` seconds for the first slurm diff.
        for key in self._cfg_by_key:
            self._event_queue.put(key, source="startup")

        while True:
            item = self._event_queue.get(timeout=float(self.cfg.reconcile_period_seconds))
            _loop_start = time.time()
            if item is None:
                # Timer fallback — full reconcile across every pool.
                pool_keys: list[str] = list(self._cfg_by_key.keys())
                source = _TIMER_SOURCE
                event_ts = _loop_start
            else:
                pool_keys, source, event_ts = [item[0]], item[1], item[2]
                _EVENT_LAG_SECONDS.labels(source=source).observe(_loop_start - event_ts)

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

            for key in pool_keys:
                cfg = self._cfg_by_key.get(key)
                if cfg is None:
                    continue
                _RECONCILES_TOTAL.labels(pool=key, source=source).inc()
                self._process_pool(cfg, all_states)

            _POLL_DURATION.observe(time.time() - _loop_start)
            pathlib.Path("/tmp/operator-alive").touch()
            pathlib.Path("/tmp/operator-ready").touch()

    # ---------- watcher threads (R21) ---------------------------------------

    def _watch_statefulsets(self) -> None:
        """Watch StatefulSet ADD/MODIFY/DELETE for our worker pools."""
        pool_keys = set(self._cfg_by_key.keys())
        while True:
            try:
                w = k8s_watch.Watch()
                for ev in w.stream(
                    self.client._apps.list_namespaced_stateful_set,
                    namespace=self.cfg.namespace,
                    timeout_seconds=300,
                ):
                    obj = ev.get("object")
                    if obj is None:
                        continue
                    name = getattr(obj.metadata, "name", "")
                    if name in pool_keys:
                        self._event_queue.put(name, source=f"k8s-sts:{ev.get('type', '?')}")
            except Exception as exc:  # noqa: BLE001
                self.logger.emit(
                    "error", level="WARN",
                    message=f"sts watch interrupted, reconnecting in 2s: {exc}",
                )
                time.sleep(2)

    def _watch_pods(self) -> None:
        """Watch worker Pods. StatefulSet pod naming is `<sts>-<ordinal>` so we
        route by name prefix instead of label selector — that survives a chart
        relabelling and stays correct without RBAC changes.
        """
        pool_keys = list(self._cfg_by_key.keys())
        while True:
            try:
                w = k8s_watch.Watch()
                for ev in w.stream(
                    self.client._core.list_namespaced_pod,
                    namespace=self.cfg.namespace,
                    timeout_seconds=300,
                ):
                    obj = ev.get("object")
                    if obj is None:
                        continue
                    pod_name = getattr(obj.metadata, "name", "")
                    for sts_name in pool_keys:
                        if pod_name.startswith(f"{sts_name}-"):
                            self._event_queue.put(sts_name, source=f"k8s-pod:{ev.get('type', '?')}")
                            break
            except Exception as exc:  # noqa: BLE001
                self.logger.emit(
                    "error", level="WARN",
                    message=f"pod watch interrupted, reconnecting in 2s: {exc}",
                )
                time.sleep(2)

    def _poll_slurm_state(self) -> None:
        """Diff Slurm state every `slurm_poll_interval_seconds`. Slurm 21.08
        has no event stream so this is the closest we get to event-driven
        for queue and node state changes.
        """
        interval = max(self.cfg.slurm_poll_interval_seconds, 0.5)
        while True:
            try:
                states = self.collector.collect_all_partition_states()
                for key, state in states.items():
                    prev = self._slurm_state_cache.get(key)
                    if prev is None or state != prev:
                        self._event_queue.put(key, source="slurm-diff")
                    self._slurm_state_cache[key] = state
            except Exception as exc:  # noqa: BLE001
                # Don't spam — circuit breaker in the main loop will surface this.
                self.logger.emit(
                    "error", level="DEBUG",
                    message=f"slurm-diff poll failed (will retry): {exc}",
                )
            time.sleep(interval)

    def _periodic_timer(self) -> None:
        """Safety-net reconcile every `reconcile_period_seconds`.

        The main consumer loop already wakes on queue.get(timeout=...) so
        this thread only exists to enqueue an explicit timer event whenever
        the queue has been quiet — which lets the timer-source metric and
        the explicit `timer` log line stay distinguishable from an idle
        consumer wake-up. The consumer treats a None dequeue as a timer.
        We don't need to enqueue here; the consumer already handles it.
        """
        # Intentionally a no-op thread today — kept as a hook so future
        # changes can switch to explicit timer events without restructuring
        # the consumer. Sleeping forever would be cleanest, but a long
        # sleep keeps the thread name visible in py-spy / faulthandler.
        while True:
            time.sleep(3600)

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
        self._draining_started.pop(key, None)
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
