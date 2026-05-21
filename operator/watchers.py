"""Watcher threads — R21 event-driven plumbing.

Each watcher runs in its own daemon thread spawned by OperatorApp.run().
They feed pool-keyed events into the same ``_PoolEventQueue`` the main
reconcile consumer drains. The consumer dedups so multiple watchers firing
the same pool key in close succession only trigger one reconcile.

Mixin pattern: these methods read ``self.client``, ``self.cfg``,
``self._cfg_by_key``, ``self._event_queue``, ``self._slurm_state_cache``,
``self.collector``, and ``self.logger`` from the OperatorApp instance.
Split out of operator/app.py in v5 review C2 to keep each concern in its
own ≤200 LoC module.
"""
from __future__ import annotations

import time

from kubernetes import watch as k8s_watch


class WatcherMixin:
    """K8s STS / Pod watchers + Slurm-state diff poller + periodic timer."""

    # ---------- K8s STS watcher ----------------------------------------------
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

    # ---------- K8s Pod watcher ----------------------------------------------
    def _watch_pods(self) -> None:
        """Watch worker Pods. StatefulSet pod naming is ``<sts>-<ordinal>`` so we
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

    # ---------- Slurm state diff poller --------------------------------------
    def _poll_slurm_state(self) -> None:
        """Diff Slurm state every ``slurm_poll_interval_seconds``. Slurm 21.08
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

    # ---------- Periodic timer (no-op hook) ----------------------------------
    def _periodic_timer(self) -> None:
        """Safety-net reconcile every ``reconcile_period_seconds``.

        The main consumer loop already wakes on ``queue.get(timeout=...)`` so
        this thread only exists to enqueue an explicit timer event whenever
        the queue has been quiet — which lets the timer-source metric and
        the explicit ``timer`` log line stay distinguishable from an idle
        consumer wake-up. The consumer treats a ``None`` dequeue as a timer.
        We don't need to enqueue here; the consumer already handles it.
        """
        # Intentionally a no-op thread today — kept as a hook so future
        # changes can switch to explicit timer events without restructuring
        # the consumer. Sleeping forever would be cleanest, but a long
        # sleep keeps the thread name visible in py-spy / faulthandler.
        while True:
            time.sleep(3600)
