"""Prometheus metric registrations.

All metrics are module-level singletons so they survive across OperatorApp
restarts within the same process.  Import individual names directly:

    from metrics import _SCALE_UP_TOTAL, _POLL_DURATION
"""

from prometheus_client import Counter, Gauge, Histogram

_SCALE_UP_TOTAL = Counter(
    "slurm_operator_scale_up_total",
    "Total scale-up actions executed, by pool",
    ["pool"],
)
_SCALE_DOWN_TOTAL = Counter(
    "slurm_operator_scale_down_total",
    "Total scale-down actions executed, by pool",
    ["pool"],
)
_SCALE_SKIPPED_TOTAL = Counter(
    "slurm_operator_scale_skipped_total",
    "Total scaling decisions skipped, by pool and reason",
    ["pool", "reason"],
)
_CHECKPOINT_GUARD_BLOCKS_TOTAL = Counter(
    "slurm_operator_checkpoint_guard_blocks_total",
    "Times checkpoint guard blocked a scale-down, by pool",
    ["pool"],
)
_POLL_DURATION = Histogram(
    "slurm_operator_poll_duration_seconds",
    "Elapsed time for one complete operator poll loop",
)
_CURRENT_REPLICAS = Gauge(
    "slurm_operator_current_replicas",
    "Current StatefulSet replica count, by pool",
    ["pool"],
)
_PODS_READY = Gauge(
    "slurm_operator_pods_ready",
    "Number of Ready pods in the pool StatefulSet",
    ["pool"],
)
_PROVISIONING_LATENCY = Histogram(
    "slurm_operator_provisioning_latency_seconds",
    "Seconds from scale-up decision to all target pods becoming Ready, by pool",
    ["pool"],
    buckets=[5, 15, 30, 60, 120, 300, 600],
)
_DRAIN_TOTAL = Counter(
    "slurm_operator_drain_total",
    "Total drain-then-wait cycles initiated before a scale-down, by pool",
    ["pool"],
)
_DRAIN_TIMEOUT_TOTAL = Counter(
    "slurm_operator_drain_timeout_total",
    "Times a draining node hit drain_timeout and was force-killed, by pool and node",
    ["pool", "node"],
)
_CIRCUIT_BREAKER_ERRORS = Gauge(
    "slurm_operator_consecutive_errors",
    "Consecutive error count in main poll loop — non-zero means circuit is open",
)
# R21: event-driven operator metrics.
_EVENT_LAG_SECONDS = Histogram(
    "slurm_operator_event_lag_seconds",
    "Seconds from when an event was enqueued (K8s watch / Slurm diff / timer) "
    "to when the reconcile actually started — measures responsiveness of the "
    "event loop. Target p95 < 1s under normal load.",
    ["source"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
)
_RECONCILES_TOTAL = Counter(
    "slurm_operator_reconciles_total",
    "Total reconcile runs by trigger source (k8s-sts / k8s-pod / slurm-diff / timer)",
    ["pool", "source"],
)
_QUEUE_DEDUP_DROPS = Counter(
    "slurm_operator_queue_dedup_drops_total",
    "Times an event was dropped because the pool already had a pending reconcile",
    ["pool", "source"],
)
# Phase 6 M7: fragmentation detector + requeue decider metrics. The gauge
# is populated each fragmentation reconcile pass; the counter increments
# only on successful (non-rate-limited) requeue decisions.
_FRAGMENTATION_SCORE = Gauge(
    "slurm_operator_fragmentation_score",
    "Coefficient of variation of free MPS slots across nodes — 0 = balanced, 1 = worst",
)
_FRAGMENTATION_BLOCKED_JOBS = Gauge(
    "slurm_operator_fragmentation_blocked_jobs",
    "Pending jobs that fit no current node at the last fragmentation snapshot",
)
_REQUEUE_TOTAL = Counter(
    "slurm_operator_requeue_total",
    "Times the operator issued a requeue decision, by reason bucket",
    ["reason"],   # "unblock" | "rate-limited" | "no-fragmentation" | "no-victims"
)
_REQUEUE_VICTIMS = Counter(
    "slurm_operator_requeue_victims_total",
    "Individual jobs requeued by the fragmentation reconciler",
)
# E7 hardening: ghost-job detector.
#
# The wedge: a worker pod is killed (helm upgrade, node eviction, OOM)
# while it owned an in-flight Slurm job. slurmctld never receives the
# job-complete epilog, so the job stays RUNNING forever in slurmrestd.
# Our scale policy sees running_jobs > 0 and refuses to scale up — even
# though no pod exists to actually run anything. Result: cluster is dead
# until a human runs `scontrol update State=DOWN/RESUME` or deletes the
# controller pod.
#
# This gauge is raised whenever the detector finds the inconsistency.
# It's intentionally separate from a counter — alerting wants "is it
# currently wedged" (gauge), tracing wants "how often does this happen"
# (we increment _GHOST_DETECTED_TOTAL too).
_GHOST_JOBS_PRESENT = Gauge(
    "slurm_operator_ghost_jobs_present",
    "1 if the pool is currently in the running_jobs>0 + replicas=0 + pods=0 wedge "
    "(an in-flight Slurm job is unreachable because its worker pod is gone)",
    ["pool"],
)
_GHOST_DETECTED_TOTAL = Counter(
    "slurm_operator_ghost_detected_total",
    "Times the operator detected ghost running jobs (no pod backing them)",
    ["pool"],
)
