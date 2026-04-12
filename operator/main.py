#!/usr/bin/env python3
"""Elastic Slurm operator — entry point.

Module layout:
  models.py    — PartitionConfig, Config, PartitionState, ScalingDecision
  metrics.py   — Prometheus metric registrations
  k8s.py       — Kubernetes API client (StatefulSet / pod exec / node drain)
  slurm.py     — Slurm REST API client (slurmrestd + JWT)
  collector.py — PartitionConfigLoader, ClusterStateCollector
  policy.py    — CheckpointAwareQueuePolicy
  app.py       — JsonLogger, StatefulSetActuator, OperatorApp (main loop)
"""

from __future__ import annotations

from app import OperatorApp
from collector import PartitionConfigLoader
from models import Config, PartitionConfig


def validate_config(cfg: Config, partition_cfgs: list[PartitionConfig]) -> None:
    if cfg.poll_interval <= 0:
        raise ValueError("POLL_INTERVAL_SECONDS must be > 0")

    seen: set[tuple[str, str]] = set()
    fallback_count = 0
    for p in partition_cfgs:
        sig = (p.partition, p.worker_statefulset)
        if sig in seen:
            raise ValueError(f"duplicate pool config: {sig}")
        seen.add(sig)
        if p.fallback:
            fallback_count += 1
        if p.min_replicas < 0 or p.max_replicas < 0:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: replicas must be >= 0")
        if p.min_replicas > p.max_replicas:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: min_replicas cannot be larger than max_replicas")
        if p.scale_up_step <= 0 or p.scale_down_step <= 0:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: scale steps must be > 0")
    if fallback_count > 1:
        raise ValueError("at most one fallback pool is allowed")


def main() -> None:
    cfg = Config()
    partition_cfgs = PartitionConfigLoader.load(cfg)
    validate_config(cfg, partition_cfgs)
    OperatorApp(cfg).run()


if __name__ == "__main__":
    main()
