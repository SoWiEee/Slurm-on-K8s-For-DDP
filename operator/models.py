"""Data classes shared across all operator modules."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PartitionConfig:
    partition: str
    worker_statefulset: str
    min_replicas: int
    max_replicas: int
    scale_up_step: int
    scale_down_step: int
    scale_down_cooldown: int
    checkpoint_path: str = ""
    max_checkpoint_age_seconds: int = 600
    checkpoint_grace_seconds: int = 0
    match_features: tuple[str, ...] = field(default_factory=tuple)
    match_gres: tuple[str, ...] = field(default_factory=tuple)
    fallback: bool = False


@dataclass(frozen=True)
class Config:
    namespace: str = os.getenv("NAMESPACE", "slurm")
    controller_pod: str = os.getenv("CONTROLLER_POD", "slurm-controller-0")
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
    policy_name: str = os.getenv("SCALING_POLICY", "checkpoint_aware_queue")
    checkpoint_guard_enabled: bool = os.getenv("CHECKPOINT_GUARD_ENABLED", "true").lower() == "true"
    default_partition: str = os.getenv("SLURM_PARTITION", "debug")
    default_worker_statefulset: str = os.getenv("WORKER_STATEFULSET", "slurm-worker-cpu")
    default_min_replicas: int = int(os.getenv("MIN_REPLICAS", "1"))
    default_max_replicas: int = int(os.getenv("MAX_REPLICAS", "4"))
    default_scale_up_step: int = int(os.getenv("SCALE_UP_STEP", "1"))
    default_scale_down_step: int = int(os.getenv("SCALE_DOWN_STEP", "1"))
    default_scale_down_cooldown: int = int(os.getenv("SCALE_DOWN_COOLDOWN_SECONDS", "60"))
    default_checkpoint_path: str = os.getenv("CHECKPOINT_PATH", "")
    default_max_checkpoint_age_seconds: int = int(os.getenv("MAX_CHECKPOINT_AGE_SECONDS", "600"))
    default_checkpoint_grace_seconds: int = int(os.getenv("CHECKPOINT_GRACE_SECONDS", "0"))
    # Slurm REST API (slurmrestd).  When set, the operator queries jobs/nodes via
    # HTTP instead of kubectl exec, eliminating fork overhead and exec timeouts.
    # Leave empty to fall back to the legacy kubectl exec path.
    slurm_rest_url: str = os.getenv("SLURM_REST_URL", "")
    slurm_rest_api_version: str = os.getenv("SLURM_REST_API_VERSION", "v0.0.37")
    # Path to the HS256 key file used to sign JWT tokens for slurmrestd.
    # Must match AuthAltParameters=jwt_key in slurm.conf.
    slurm_jwt_key_path: str = os.getenv("SLURM_JWT_KEY_PATH", "")


@dataclass(frozen=True)
class PartitionState:
    partition: str
    worker_statefulset: str
    current_replicas: int
    pending_jobs: int
    running_jobs: int
    busy_nodes: int


@dataclass(frozen=True)
class ScalingDecision:
    target_replicas: int
    action: str
    reason: str
