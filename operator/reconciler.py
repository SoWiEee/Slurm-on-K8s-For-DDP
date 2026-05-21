"""Per-pool reconcile — the core decision loop.

_process_pool runs once per pool key per reconcile event. It pulls cluster
state, evaluates the policy, optionally updates OTel job-lifecycle spans
(queue_wait → job_running), and dispatches the decision to one of
ScaleActionsMixin's actuators (_do_scale_up / _do_scale_down / _do_keep).

Mixin pattern: reads ``self.client``, ``self.cfg``, ``self.policy``,
``self.collector``, ``self.rest``, ``self._provisioning``,
``self._checkpoint_missing_since``, ``self._job_pending_trace``,
``self._job_running_trace``, ``self.last_scale_up_at``, ``self.logger``.
Split out of operator/app.py in v5 review C2.
"""
from __future__ import annotations

import time
from dataclasses import asdict

import otel as _otel

from metrics import (
    _CURRENT_REPLICAS,
    _GHOST_DETECTED_TOTAL,
    _GHOST_JOBS_PRESENT,
    _PODS_READY,
    _PROVISIONING_LATENCY,
)
from models import PartitionConfig


class ReconcilerMixin:
    """Per-pool reconcile entry point."""

    def _process_pool(self, partition_cfg: PartitionConfig, all_states: dict) -> None:
        key = partition_cfg.worker_statefulset
        try:
            state = all_states[key]
            _CURRENT_REPLICAS.labels(pool=key).set(state.current_replicas)

            # Pods-ready gauge + provisioning latency tracking
            ready = self.client.get_ready_replicas(key)
            _PODS_READY.labels(pool=key).set(ready)
            if key in self._provisioning:
                prov_start, prov_target, prov_ctx = self._provisioning[key]
                if ready >= prov_target:
                    latency = time.time() - prov_start
                    if _otel.enabled():
                        with _otel.start_span(
                            "k8s_provisioning",
                            parent_context=prov_ctx,
                            attributes={
                                "pool": key,
                                "target_replicas": prov_target,
                                "latency_seconds": latency,
                            },
                            start_time_ns=int(prov_start * 1e9),
                        ):
                            tid = _otel.current_trace_id()
                        _PROVISIONING_LATENCY.labels(pool=key).observe(
                            latency,
                            exemplar={"traceID": tid} if tid else None,
                        )
                    else:
                        _PROVISIONING_LATENCY.labels(pool=key).observe(latency)
                    del self._provisioning[key]

            # E7 hardening — ghost-job detector. If slurmrestd reports
            # running jobs but the StatefulSet has scaled to zero AND no
            # pod is Ready, those "running" jobs are orphaned: their
            # worker pod died without slurmd reporting the epilog, so
            # the controller's job table is wedged. Left untreated this
            # blocks every future scale-up (no pending → scheduler hands
            # off → no pod → loop). We surface it via metric + log so
            # alerting can page; we deliberately don't auto-recover
            # because the safe recovery (scontrol DOWN/RESUME on each
            # node, or ``kubectl delete pod slurm-controller-0``) has a
            # non-trivial blast radius if mis-fired during a real burst.
            ghost = (state.current_replicas == 0 and ready == 0
                     and state.running_jobs > 0)
            _GHOST_JOBS_PRESENT.labels(pool=key).set(1 if ghost else 0)
            if ghost:
                _GHOST_DETECTED_TOTAL.labels(pool=key).inc()
                self.logger.emit(
                    "ghost_jobs_detected",
                    policy=self.cfg.policy_name,
                    partition=partition_cfg.partition,
                    statefulset=key,
                    running_jobs=state.running_jobs,
                    current_replicas=state.current_replicas,
                    ready_replicas=ready,
                    severity="warning",
                    remediation=(
                        "scontrol update NodeName=<pool>-[0-N] State=DOWN; "
                        "sleep 2; State=RESUME — or kubectl delete pod "
                        "slurm-controller-0 to force slurmctld state rebuild. "
                        "See docs/note.md #16."
                    ),
                )

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

            # OTel: track job lifecycle spans (queue_wait → job_running).
            if _otel.enabled() and self.rest is not None:
                raw_jobs = self.rest.list_jobs(partition_cfg.partition)
                visible_ids = {str(j.get("job_id", "")) for j in raw_jobs}

                for jraw in raw_jobs:
                    jid = str(jraw.get("job_id", ""))
                    jstate = jraw.get("job_state", "")
                    if not jid:
                        continue

                    if jstate == "PENDING" and jid not in self._job_pending_trace:
                        # First time seeing this PENDING job — try to read traceparent.
                        comment = self.rest.get_job_admin_comment(jid)
                        tp = ""
                        for part in (comment or "").split(";"):
                            if part.strip().startswith("otel="):
                                tp = part.strip()[5:]
                                break
                        parent_ctx = _otel.extract_context(tp) if tp else None
                        span_ctx = _otel.start_span(
                            "queue_wait",
                            parent_context=parent_ctx,
                            attributes={
                                "job_id": jid,
                                "partition": partition_cfg.partition,
                                "pending_jobs": state.pending_jobs,
                            },
                        )
                        span_ctx.__enter__()
                        self._job_pending_trace[jid] = (tp, span_ctx)

                    elif jstate == "RUNNING":
                        if jid in self._job_pending_trace:
                            # Transition PENDING → RUNNING: close queue_wait, open job_running.
                            tp, qw_ctx = self._job_pending_trace.pop(jid)
                            qw_ctx.__exit__(None, None, None)
                            parent_ctx = _otel.extract_context(tp) if tp else None
                            run_ctx = _otel.start_span(
                                "job_running",
                                parent_context=parent_ctx,
                                attributes={
                                    "job_id": jid,
                                    "partition": partition_cfg.partition,
                                    "nodes": jraw.get("nodes", ""),
                                    "cpus": jraw.get("cpus", {}).get("allocated", 0)
                                        if isinstance(jraw.get("cpus"), dict)
                                        else jraw.get("num_cpus", 0),
                                    "gres": str(jraw.get("gres_detail", "")),
                                },
                            )
                            run_ctx.__enter__()
                            self._job_running_trace[jid] = (tp, run_ctx)
                        elif jid not in self._job_running_trace:
                            # Job was already RUNNING when operator started — open span without parent.
                            run_ctx = _otel.start_span(
                                "job_running",
                                attributes={
                                    "job_id": jid,
                                    "partition": partition_cfg.partition,
                                    "nodes": jraw.get("nodes", ""),
                                },
                            )
                            run_ctx.__enter__()
                            self._job_running_trace[jid] = ("", run_ctx)

                # Close spans for jobs that have left the visible set.
                for jid in list(self._job_pending_trace):
                    if jid not in visible_ids:
                        _, span_ctx = self._job_pending_trace.pop(jid)
                        span_ctx.__exit__(None, None, None)
                for jid in list(self._job_running_trace):
                    if jid not in visible_ids:
                        _, span_ctx = self._job_running_trace.pop(jid)
                        span_ctx.__exit__(None, None, None)

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
