#!/usr/bin/env python3
"""Phase 2 elastic scaler.

Milestone A + B implementation:
- Equivalent control behavior with clearer architecture (Collector / Policy / Actuator).
- Structured JSON logs for observation, decisions, actions and errors.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class Config:
    namespace: str = os.getenv("NAMESPACE", "slurm")
    controller_pod: str = os.getenv("CONTROLLER_POD", "slurm-controller-0")
    worker_statefulset: str = os.getenv("WORKER_STATEFULSET", "slurm-worker")
    min_replicas: int = int(os.getenv("MIN_REPLICAS", "1"))
    max_replicas: int = int(os.getenv("MAX_REPLICAS", "3"))
    scale_up_step: int = int(os.getenv("SCALE_UP_STEP", "1"))
    scale_down_step: int = int(os.getenv("SCALE_DOWN_STEP", "1"))
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
    scale_down_cooldown: int = int(os.getenv("SCALE_DOWN_COOLDOWN_SECONDS", "60"))
    policy_name: str = os.getenv("SCALING_POLICY", "basic_queue")


@dataclass(frozen=True)
class ClusterState:
    current_replicas: int
    pending_jobs: int
    busy_nodes: int


@dataclass(frozen=True)
class ScalingDecision:
    target_replicas: int
    action: str  # scale_up | scale_down | keep
    reason: str


class JsonLogger:
    """Simple JSON-lines logger for operator events."""

    def emit(self, event_type: str, level: str = "INFO", **fields: Any) -> None:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "event_type": event_type,
            **fields,
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


class KubectlClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def run(self, args: Iterable[str]) -> str:
        cmd = ["kubectl", *args]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def exec_in_controller(self, command: str) -> str:
        return self.run(
            [
                "-n",
                self.cfg.namespace,
                "exec",
                f"pod/{self.cfg.controller_pod}",
                "--",
                "bash",
                "-lc",
                command,
            ]
        )


class ClusterStateCollector:
    """Collects current cluster/slurm signals without making decisions."""

    def __init__(self, cfg: Config, client: KubectlClient):
        self.cfg = cfg
        self.client = client

    def get_current_replicas(self) -> int:
        output = self.client.run(
            [
                "-n",
                self.cfg.namespace,
                "get",
                "statefulset",
                self.cfg.worker_statefulset,
                "-o",
                "json",
            ]
        )
        payload = json.loads(output)
        return int(payload.get("spec", {}).get("replicas", 0))

    def get_pending_jobs(self) -> int:
        output = self.client.exec_in_controller("squeue -h -t PENDING | wc -l")
        return int(output or "0")

    def get_busy_nodes(self) -> int:
        # ALLOCATED/MIXED/COMPLETING are considered busy.
        output = self.client.exec_in_controller(
            r"sinfo -h -N -o '%T' | egrep -E 'ALLOCATED|MIXED|COMPLETING' | wc -l || true"
        )
        return int(output or "0")

    def collect(self) -> ClusterState:
        return ClusterState(
            current_replicas=self.get_current_replicas(),
            pending_jobs=self.get_pending_jobs(),
            busy_nodes=self.get_busy_nodes(),
        )


class BasicQueuePolicy:
    """Equivalent to previous Phase 2 decision logic."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def evaluate(self, state: ClusterState) -> ScalingDecision:
        if state.pending_jobs > 0:
            target = clamp(
                state.current_replicas + self.cfg.scale_up_step,
                self.cfg.min_replicas,
                self.cfg.max_replicas,
            )
            return self._to_decision(state.current_replicas, target, "pending_jobs")

        safe_floor = max(self.cfg.min_replicas, state.busy_nodes)
        target = clamp(
            state.current_replicas - self.cfg.scale_down_step,
            safe_floor,
            self.cfg.max_replicas,
        )
        return self._to_decision(state.current_replicas, target, "no_pending_jobs")

    @staticmethod
    def _to_decision(current: int, target: int, reason: str) -> ScalingDecision:
        if target > current:
            action = "scale_up"
        elif target < current:
            action = "scale_down"
        else:
            action = "keep"
        return ScalingDecision(target_replicas=target, action=action, reason=reason)


class StatefulSetActuator:
    """Applies scaling decision to Kubernetes."""

    def __init__(self, cfg: Config, client: KubectlClient):
        self.cfg = cfg
        self.client = client

    def patch_replicas(self, replicas: int) -> None:
        self.client.run(
            [
                "-n",
                self.cfg.namespace,
                "patch",
                "statefulset",
                self.cfg.worker_statefulset,
                "--type=merge",
                "-p",
                json.dumps({"spec": {"replicas": replicas}}),
            ]
        )


def validate_config(cfg: Config) -> None:
    if cfg.min_replicas < 0 or cfg.max_replicas < 0:
        raise ValueError("replicas must be >= 0")
    if cfg.min_replicas > cfg.max_replicas:
        raise ValueError("MIN_REPLICAS cannot be larger than MAX_REPLICAS")


class OperatorApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.logger = JsonLogger()
        self.client = KubectlClient(cfg)
        self.collector = ClusterStateCollector(cfg, self.client)
        self.policy = BasicQueuePolicy(cfg)
        self.actuator = StatefulSetActuator(cfg, self.client)
        self.last_scale_up_at = 0.0

    def run(self) -> None:
        self.logger.emit(
            "startup",
            policy=self.cfg.policy_name,
            config=asdict(self.cfg),
        )

        while True:
            try:
                state = self.collector.collect()
                decision = self.policy.evaluate(state)

                now = time.time()
                cooldown_elapsed = now - self.last_scale_up_at
                cooldown_remaining = max(
                    self.cfg.scale_down_cooldown - int(cooldown_elapsed),
                    0,
                )

                self.logger.emit(
                    "loop_observation",
                    policy=self.cfg.policy_name,
                    state=asdict(state),
                    decision=asdict(decision),
                    cooldown_remaining_seconds=cooldown_remaining,
                )

                if decision.action == "scale_up":
                    self.actuator.patch_replicas(decision.target_replicas)
                    self.last_scale_up_at = now
                    self.logger.emit(
                        "scale_action",
                        policy=self.cfg.policy_name,
                        action="scale_up",
                        from_replicas=state.current_replicas,
                        to_replicas=decision.target_replicas,
                        reason=decision.reason,
                        pending_jobs=state.pending_jobs,
                        busy_nodes=state.busy_nodes,
                    )
                elif decision.action == "scale_down":
                    if cooldown_elapsed >= self.cfg.scale_down_cooldown:
                        self.actuator.patch_replicas(decision.target_replicas)
                        self.logger.emit(
                            "scale_action",
                            policy=self.cfg.policy_name,
                            action="scale_down",
                            from_replicas=state.current_replicas,
                            to_replicas=decision.target_replicas,
                            reason=decision.reason,
                            pending_jobs=state.pending_jobs,
                            busy_nodes=state.busy_nodes,
                        )
                    else:
                        self.logger.emit(
                            "scale_skipped",
                            policy=self.cfg.policy_name,
                            action="scale_down",
                            from_replicas=state.current_replicas,
                            to_replicas=decision.target_replicas,
                            reason="scale_down_cooldown",
                            cooldown_remaining_seconds=cooldown_remaining,
                            pending_jobs=state.pending_jobs,
                            busy_nodes=state.busy_nodes,
                        )
                else:
                    self.logger.emit(
                        "scale_skipped",
                        policy=self.cfg.policy_name,
                        action="keep",
                        from_replicas=state.current_replicas,
                        to_replicas=decision.target_replicas,
                        reason=decision.reason,
                        pending_jobs=state.pending_jobs,
                        busy_nodes=state.busy_nodes,
                    )
            except Exception as exc:  # noqa: BLE001
                self.logger.emit("error", level="ERROR", message=str(exc))

            time.sleep(self.cfg.poll_interval)


def main() -> None:
    cfg = Config()
    validate_config(cfg)
    OperatorApp(cfg).run()


if __name__ == "__main__":
    main()
