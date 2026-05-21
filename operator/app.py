"""Operator application — event-driven reconcile loop (R21).

OperatorApp wires together all components and runs three watcher threads
plus a single reconcile consumer:

  K8s StatefulSet watch ──┐
  K8s Pod watch ──────────┼──► dedup queue ──► reconcile(pool) ──► actuate
  Slurm 1s state diff ────┤                       ▲
  60s timer (safety net) ─┘                       │
                                                  │
                            measure event_lag_seconds (event_ts → reconcile_start)

The previous polling-only loop was rebuilt around a ``_PoolEventQueue`` so
scale events trigger within sub-second of being observed by the K8s API
or by squeue, while a 60s timer-driven reconcile runs as a safety net so
a missed watch event cannot leave a pool stuck out of sync.

This module owns:
  - OperatorApp.__init__       (wiring + state restore from annotations)
  - OperatorApp.run            (startup + main consumer loop + circuit breaker)
  - _PoolEventQueue            (pool-keyed dedup queue)
  - JsonLogger                 (structured log emitter)
  - StatefulSetActuator        (thin wrapper around K8sClient.patch_replicas)

The watcher threads, reconcile dispatcher, scale actuators, and
fragmentation loop live in dedicated modules and are mixed into
OperatorApp via inheritance — see v5 review C2 for the refactor rationale.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from prometheus_client import start_http_server

from collector import ClusterStateCollector, PartitionConfigLoader
from fragmentation import (
    FragmentationDetector,
    FragmentationReconciler,
    RequeueDecider,
)
from fragmentation_loop import FragmentationLoopMixin
from k8s import K8sClient
from metrics import (
    _CIRCUIT_BREAKER_ERRORS,
    _EVENT_LAG_SECONDS,
    _POLL_DURATION,
    _QUEUE_DEDUP_DROPS,
    _RECONCILES_TOTAL,
)
from models import Config, PartitionConfig, PartitionState
from policy import CheckpointAwareQueuePolicy
from reconciler import ReconcilerMixin
from scale_actions import _COOLDOWN_ANNOTATION, ScaleActionsMixin
from slurm import SlurmRestClient
from watchers import WatcherMixin

_TIMER_SOURCE = "timer"


class _PoolEventQueue:
    """Pool-keyed work queue with dedup.

    Multiple watchers (K8s STS, K8s Pod, Slurm diff, periodic timer) can
    enqueue the same pool key simultaneously; the consumer only needs to
    reconcile each pool once per "burst" so we collapse pending entries.

    ``put`` returns False (and increments a metric) if the pool already has
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


class OperatorApp(
    WatcherMixin,
    ReconcilerMixin,
    ScaleActionsMixin,
    FragmentationLoopMixin,
):
    """Multi-pool elastic operator.

    Mixins provide:
      - WatcherMixin           : _watch_statefulsets / _watch_pods /
                                 _poll_slurm_state / _periodic_timer
      - ReconcilerMixin        : _process_pool (the per-pool decision loop)
      - ScaleActionsMixin      : _do_scale_up / _do_scale_down / _do_keep
      - FragmentationLoopMixin : _fragmentation_loop / _fragmentation_tick
    """

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
        # Track pending provisioning: pool → (scale_up_timestamp, target_replicas, otel_ctx).
        # Cleared once readyReplicas reaches the target; used to emit _PROVISIONING_LATENCY.
        self._provisioning: dict[str, tuple[float, int, Any]] = {}
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
        # OTel: job_id → (traceparent str, open queue_wait span ctx).
        # Populated when we first see a PENDING job with an OTel admin_comment.
        self._job_pending_trace: dict[str, tuple[str, Any]] = {}
        # OTel: job_id → open job_running span ctx.
        # Populated when a tracked job enters RUNNING; closed when it leaves.
        self._job_running_trace: dict[str, Any] = {}
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

        # Phase 6 M7: fragmentation reconciler (default disabled, default
        # shadow=true even when enabled — flip FRAGMENTATION_SHADOW_MODE=false
        # only after observing decisions in shadow mode for a release cycle).
        self._fragmentation_interval = float(
            os.getenv("FRAGMENTATION_MIN_INTERVAL_SECONDS", "60")
        )
        self._fragmentation_partitions = tuple(
            p.strip() for p in os.getenv("FRAGMENTATION_PARTITIONS", "").split(",")
            if p.strip()
        )
        self._fragmentation_reconciler = self._build_fragmentation_reconciler()

    def _build_fragmentation_reconciler(self) -> FragmentationReconciler | None:
        if os.getenv("FRAGMENTATION_ENABLED", "false").lower() != "true":
            return None
        if self.rest is None:
            # The reconciler depends on Slurm REST for jobs/nodes — exec
            # path doesn't expose the per-job priority + gres_used we need.
            # Disable rather than silently fall back to a degraded view.
            return None
        mps_per_node = int(os.getenv("FRAGMENTATION_MPS_PER_NODE", "100"))
        priority_gap = int(os.getenv("FRAGMENTATION_PRIORITY_GAP", "0"))
        max_per_hour = int(os.getenv("FRAGMENTATION_MAX_REQUEUES_PER_HOUR", "5"))
        max_targets = int(os.getenv("FRAGMENTATION_MAX_TARGETS_PER_DECISION", "4"))
        shadow = os.getenv("FRAGMENTATION_SHADOW_MODE", "true").lower() == "true"
        detector = FragmentationDetector(
            mps_per_node=mps_per_node, priority_gap=priority_gap,
        )
        decider = RequeueDecider(
            min_interval_seconds=self._fragmentation_interval,
            max_requeues_per_hour=max_per_hour,
            max_targets_per_decision=max_targets,
        )
        return FragmentationReconciler(
            detector=detector,
            decider=decider,
            actuator=self._fragmentation_actuator,
            shadow_mode=shadow,
        )

    def _fragmentation_actuator(self, job_id: str) -> None:
        """Issue ``scontrol requeue`` against a single Slurm job id."""
        self.client.exec_in_controller(f"scontrol requeue {job_id}")

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
        # M7: separate fragmentation reconcile thread. Stays out of the
        # main reconcile queue so a slow scontrol requeue can't delay
        # scale-up / scale-down decisions, and so its rate-limit window
        # is independent of pool reconciles.
        if self._fragmentation_reconciler is not None:
            t = threading.Thread(
                target=self._fragmentation_loop,
                name="fragmentation-reconciler",
                daemon=True,
            )
            t.start()
            self.logger.emit(
                "fragmentation_started",
                interval_seconds=self._fragmentation_interval,
                shadow_mode=self._fragmentation_reconciler.shadow_mode,
                partitions=list(self._fragmentation_partitions) or "all",
            )
        # Prime the queue so the first reconcile happens immediately, before
        # any watcher fires. Without this the readiness probe waits up to
        # ``slurm_poll_interval`` seconds for the first slurm diff.
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
