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
_CIRCUIT_BREAKER_ERRORS = Gauge(
    "slurm_operator_consecutive_errors",
    "Consecutive error count in main poll loop — non-zero means circuit is open",
)
