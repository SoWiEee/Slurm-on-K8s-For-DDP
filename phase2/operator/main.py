#!/usr/bin/env python3
"""Phase 2 elastic scaler.

Milestone C + D implementation:
- Partition-aware independent scaling.
- Checkpoint-aware scale-down guard.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


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


@dataclass(frozen=True)
class Config:
    namespace: str = os.getenv("NAMESPACE", "slurm")
    controller_pod: str = os.getenv("CONTROLLER_POD", "slurm-controller-0")
    slurm_configmap: str = os.getenv("SLURM_CONFIGMAP", "slurm-config")
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
    policy_name: str = os.getenv("SCALING_POLICY", "checkpoint_aware_queue")
    checkpoint_guard_enabled: bool = os.getenv("CHECKPOINT_GUARD_ENABLED", "true").lower() == "true"
    # For single-partition fallback.
    default_partition: str = os.getenv("SLURM_PARTITION", "debug")
    default_worker_statefulset: str = os.getenv("WORKER_STATEFULSET", "slurm-worker")
    default_min_replicas: int = int(os.getenv("MIN_REPLICAS", "1"))
    default_max_replicas: int = int(os.getenv("MAX_REPLICAS", "3"))
    default_scale_up_step: int = int(os.getenv("SCALE_UP_STEP", "1"))
    default_scale_down_step: int = int(os.getenv("SCALE_DOWN_STEP", "1"))
    default_scale_down_cooldown: int = int(os.getenv("SCALE_DOWN_COOLDOWN_SECONDS", "60"))
    default_checkpoint_path: str = os.getenv("CHECKPOINT_PATH", "")
    default_max_checkpoint_age_seconds: int = int(os.getenv("MAX_CHECKPOINT_AGE_SECONDS", "600"))


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
    action: str  # scale_up | scale_down | keep
    reason: str


class JsonLogger:
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


    def get_configmap_slurm_conf(self) -> str:
        # slurm.conf is stored in ConfigMap data under key "slurm.conf"
        return self.run(
            [
                "-n",
                self.cfg.namespace,
                "get",
                "configmap",
                self.cfg.slurm_configmap,
                "-o",
                "jsonpath={.data.slurm\.conf}",
            ]
        )

    def patch_configmap_slurm_conf(self, new_conf: str) -> None:
        patch = {"data": {"slurm.conf": new_conf}}
        self.run(
            [
                "-n",
                self.cfg.namespace,
                "patch",
                "configmap",
                self.cfg.slurm_configmap,
                "--type",
                "merge",
                "-p",
                json.dumps(patch),
            ]
        )



def render_slurm_conf_for_replicas(existing_conf: str, partition: str, replicas: int) -> str:
    """
    Minimal "root-cause" fix for NO NETWORK ADDRESS spam:
    keep slurm.conf NodeName/Partition Nodes aligned with *current* k8s replicas.

    When slurm.conf lists NodeName entries for workers that don't exist as pods,
    slurmctld will continuously try to resolve them and log:
      get_addr_info ... Unable to resolve ... NO NETWORK ADDRESS FOUND

    Strategy:
      - Use the first existing slurm-worker NodeName line as a template.
      - Rewrite NodeName lines to exactly slurm-worker-0..slurm-worker-(replicas-1).
      - Rewrite PartitionName=<partition> Nodes=... accordingly.
    """
    replicas = max(1, int(replicas))

    lines = existing_conf.splitlines()

    # Find a template line (prefer worker-0)
    tmpl = None
    for l in lines:
        if l.strip().startswith("NodeName=slurm-worker-0 "):
            tmpl = l
            break
    if tmpl is None:
        for l in lines:
            if l.strip().startswith("NodeName=slurm-worker-"):
                tmpl = l
                break
    if tmpl is None:
        # Can't safely rewrite; return original
        return existing_conf

    # Remove existing slurm-worker NodeName lines
    kept: list[str] = []
    for l in lines:
        if l.strip().startswith("NodeName=slurm-worker-"):
            continue
        kept.append(l)

    def render_node_line(i: int) -> str:
        out = tmpl
        out = re.sub(r"NodeName=slurm-worker-\d+", f"NodeName=slurm-worker-{i}", out)
        out = re.sub(
            r"NodeAddr=slurm-worker-\d+\.slurm-worker\.slurm\.svc\.cluster\.local",
            f"NodeAddr=slurm-worker-{i}.slurm-worker.slurm.svc.cluster.local",
            out,
        )
        out = re.sub(r"NodeHostname=slurm-worker-\d+", f"NodeHostname=slurm-worker-{i}", out)
        return out

    node_lines = [render_node_line(i) for i in range(replicas)]

    # Insert NodeName lines near the original template position (best-effort)
    insert_at = 0
    for idx, l in enumerate(kept):
        if l.strip().startswith("CryptoType="):
            insert_at = idx + 1
            break
    # Keep a blank line before nodes for readability
    kept = kept[:insert_at] + [""] + node_lines + [""] + kept[insert_at:]

    # Rewrite PartitionName Nodes=... for the target partition
    nodes_expr = f"slurm-worker-[0-{replicas-1}]" if replicas > 1 else "slurm-worker-0"
    out_lines: list[str] = []
    part_re = re.compile(rf"^(PartitionName={re.escape(partition)}\b.*\bNodes=)(\S+)(.*)$")
    for l in kept:
        m = part_re.match(l.strip())
        if m:
            out_lines.append(f"{m.group(1)}{nodes_expr}{m.group(3)}")
        else:
            out_lines.append(l)

    # Ensure trailing newline
    return "\n".join(out_lines).rstrip() + "\n"


class PartitionConfigLoader:
    @staticmethod
    def load(cfg: Config) -> list[PartitionConfig]:
        raw = os.getenv("PARTITIONS_JSON", "").strip()
        if not raw:
            return [
                PartitionConfig(
                    partition=cfg.default_partition,
                    worker_statefulset=cfg.default_worker_statefulset,
                    min_replicas=cfg.default_min_replicas,
                    max_replicas=cfg.default_max_replicas,
                    scale_up_step=cfg.default_scale_up_step,
                    scale_down_step=cfg.default_scale_down_step,
                    scale_down_cooldown=cfg.default_scale_down_cooldown,
                    checkpoint_path=cfg.default_checkpoint_path,
                    max_checkpoint_age_seconds=cfg.default_max_checkpoint_age_seconds,
                )
            ]

        payload = json.loads(raw)
        if not isinstance(payload, list) or not payload:
            raise ValueError("PARTITIONS_JSON must be a non-empty JSON array")

        partitions: list[PartitionConfig] = []
        for item in payload:
            partitions.append(
                PartitionConfig(
                    partition=item["partition"],
                    worker_statefulset=item["worker_statefulset"],
                    min_replicas=int(item.get("min_replicas", 1)),
                    max_replicas=int(item.get("max_replicas", 1)),
                    scale_up_step=int(item.get("scale_up_step", 1)),
                    scale_down_step=int(item.get("scale_down_step", 1)),
                    scale_down_cooldown=int(item.get("scale_down_cooldown", 60)),
                    checkpoint_path=item.get("checkpoint_path", ""),
                    max_checkpoint_age_seconds=int(item.get("max_checkpoint_age_seconds", 600)),
                )
            )
        return partitions


class ClusterStateCollector:
    def __init__(self, client: KubectlClient):
        self.client = client

    def get_current_replicas(self, statefulset: str) -> int:
        output = self.client.run(
            ["-n", self.client.cfg.namespace, "get", "statefulset", statefulset, "-o", "json"]
        )
        payload = json.loads(output)
        return int(payload.get("spec", {}).get("replicas", 0))

    def get_pending_jobs(self, partition: str) -> int:
        output = self.client.exec_in_controller(f"squeue -h -t PENDING -p {partition} | wc -l")
        return int(output or "0")

    def get_running_jobs(self, partition: str) -> int:
        output = self.client.exec_in_controller(f"squeue -h -t RUNNING -p {partition} | wc -l")
        return int(output or "0")

    def get_busy_nodes(self, partition: str) -> int:
        output = self.client.exec_in_controller(
            rf"sinfo -h -p {partition} -N -o '%T' | egrep -E 'ALLOCATED|MIXED|COMPLETING' | wc -l || true"
        )
        return int(output or "0")

    def get_checkpoint_age_seconds(self, checkpoint_path: str) -> int | None:
        if not checkpoint_path:
            return None
        command = (
            f"if [ -f '{checkpoint_path}' ]; then "
            f"now=$(date +%s); mtime=$(stat -c %Y '{checkpoint_path}'); "
            "echo $((now - mtime)); else echo -1; fi"
        )
        output = self.client.exec_in_controller(command)
        age = int(output or "-1")
        if age < 0:
            return None
        return age

    def collect_partition_state(self, p: PartitionConfig) -> PartitionState:
        return PartitionState(
            partition=p.partition,
            worker_statefulset=p.worker_statefulset,
            current_replicas=self.get_current_replicas(p.worker_statefulset),
            pending_jobs=self.get_pending_jobs(p.partition),
            running_jobs=self.get_running_jobs(p.partition),
            busy_nodes=self.get_busy_nodes(p.partition),
        )


class CheckpointAwareQueuePolicy:
    def __init__(self, guard_enabled: bool):
        self.guard_enabled = guard_enabled

    def evaluate(
        self,
        partition_cfg: PartitionConfig,
        state: PartitionState,
        checkpoint_age_seconds: int | None,
    ) -> ScalingDecision:
        if state.pending_jobs > 0:
            target = clamp(
                state.current_replicas + partition_cfg.scale_up_step,
                partition_cfg.min_replicas,
                partition_cfg.max_replicas,
            )
            return self._to_decision(state.current_replicas, target, "pending_jobs")

        safe_floor = max(partition_cfg.min_replicas, state.busy_nodes)
        candidate_target = clamp(
            state.current_replicas - partition_cfg.scale_down_step,
            safe_floor,
            partition_cfg.max_replicas,
        )

        if candidate_target < state.current_replicas and self.guard_enabled and state.running_jobs > 0:
            if checkpoint_age_seconds is None:
                return ScalingDecision(
                    target_replicas=state.current_replicas,
                    action="keep",
                    reason="checkpoint_unknown_block_scale_down",
                )
            if checkpoint_age_seconds > partition_cfg.max_checkpoint_age_seconds:
                return ScalingDecision(
                    target_replicas=state.current_replicas,
                    action="keep",
                    reason="checkpoint_stale_block_scale_down",
                )

        return self._to_decision(state.current_replicas, candidate_target, "no_pending_jobs")

    @staticmethod
    def _to_decision(current: int, target: int, reason: str) -> ScalingDecision:
        if target > current:
            return ScalingDecision(target_replicas=target, action="scale_up", reason=reason)
        if target < current:
            return ScalingDecision(target_replicas=target, action="scale_down", reason=reason)
        return ScalingDecision(target_replicas=target, action="keep", reason=reason)


class StatefulSetActuator:
    def __init__(self, client: KubectlClient):
        self.client = client

    def patch_replicas(self, statefulset: str, replicas: int) -> None:
        self.client.run(
            [
                "-n",
                self.client.cfg.namespace,
                "patch",
                "statefulset",
                statefulset,
                "--type=merge",
                "-p",
                json.dumps({"spec": {"replicas": replicas}}),
            ]
        )


class OperatorApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.logger = JsonLogger()
        self.client = KubectlClient(cfg)
        self.collector = ClusterStateCollector(self.client)
        self.policy = CheckpointAwareQueuePolicy(cfg.checkpoint_guard_enabled)
        self.actuator = StatefulSetActuator(self.client)
        self.partition_cfgs = PartitionConfigLoader.load(cfg)
        self.last_scale_up_at: dict[str, float] = {p.partition: 0.0 for p in self.partition_cfgs}

    def run(self) -> None:
        self.logger.emit(
            "startup",
            policy=self.cfg.policy_name,
            config=asdict(self.cfg),
            partitions=[asdict(p) for p in self.partition_cfgs],
        )

        while True:
            for partition_cfg in self.partition_cfgs:
                partition = partition_cfg.partition
                try:
                    state = self.collector.collect_partition_state(partition_cfg)
                    checkpoint_age = self.collector.get_checkpoint_age_seconds(partition_cfg.checkpoint_path)
                    decision = self.policy.evaluate(partition_cfg, state, checkpoint_age)

                    now = time.time()
                    cooldown_elapsed = now - self.last_scale_up_at[partition]
                    cooldown_remaining = max(partition_cfg.scale_down_cooldown - int(cooldown_elapsed), 0)

                    self.logger.emit(
                        "loop_observation",
                        policy=self.cfg.policy_name,
                        partition=partition,
                        state=asdict(state),
                        decision=asdict(decision),
                        checkpoint_age_seconds=checkpoint_age,
                        cooldown_remaining_seconds=cooldown_remaining,
                    )

                    if decision.action == "scale_up":
                        self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
                        self.last_scale_up_at[partition] = now
                        self.logger.emit(
                            "scale_action",
                            policy=self.cfg.policy_name,
                            partition=partition,
                            action="scale_up",
                            statefulset=partition_cfg.worker_statefulset,
                            from_replicas=state.current_replicas,
                            to_replicas=decision.target_replicas,
                            reason=decision.reason,
                            pending_jobs=state.pending_jobs,
                            running_jobs=state.running_jobs,
                            busy_nodes=state.busy_nodes,
                        )
                    elif decision.action == "scale_down":
                        if cooldown_elapsed >= partition_cfg.scale_down_cooldown:
                            self.actuator.patch_replicas(
                                partition_cfg.worker_statefulset,
                                decision.target_replicas,
                            )
                            self.logger.emit(
                                "scale_action",
                                policy=self.cfg.policy_name,
                                partition=partition,
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
                            self.logger.emit(
                                "scale_skipped",
                                policy=self.cfg.policy_name,
                                partition=partition,
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
                    else:
                        self.logger.emit(
                            "scale_skipped",
                            policy=self.cfg.policy_name,
                            partition=partition,
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

                    # Root-cause fix: keep slurm.conf in sync with current k8s replicas.
                    # Otherwise slurmctld will keep trying to resolve non-existent worker FQDNs
                    # and nodes will get stuck with NO NETWORK ADDRESS FOUND.
                    try:
                        applied_replicas = state.current_replicas
                        if decision.action == "scale_up":
                            applied_replicas = decision.target_replicas
                        elif decision.action == "scale_down" and cooldown_elapsed >= partition_cfg.scale_down_cooldown:
                            applied_replicas = decision.target_replicas

                        existing_conf = self.client.get_configmap_slurm_conf()
                        new_conf = render_slurm_conf_for_replicas(existing_conf, partition_cfg.partition, applied_replicas)
                        if new_conf != existing_conf:
                            self.client.patch_configmap_slurm_conf(new_conf)
                            # Reload slurmctld so it stops probing removed nodes immediately
                            self.client.exec_in_controller("scontrol reconfigure || true")
                            self.logger.emit(
                                "slurm_conf_reconciled",
                                policy=self.cfg.policy_name,
                                partition=partition,
                                replicas=applied_replicas,
                                configmap=self.cfg.slurm_configmap,
                            )
                    except Exception as _exc:  # noqa: BLE001
                        self.logger.emit(
                            "slurm_conf_reconcile_failed",
                            level="WARN",
                            partition=partition,
                            message=str(_exc),
                        )

                except Exception as exc:  # noqa: BLE001
                    self.logger.emit("error", level="ERROR", partition=partition, message=str(exc))

            time.sleep(self.cfg.poll_interval)


def validate_config(cfg: Config, partition_cfgs: list[PartitionConfig]) -> None:
    if cfg.poll_interval <= 0:
        raise ValueError("POLL_INTERVAL_SECONDS must be > 0")

    seen: set[str] = set()
    for p in partition_cfgs:
        if p.partition in seen:
            raise ValueError(f"duplicate partition in config: {p.partition}")
        seen.add(p.partition)

        if p.min_replicas < 0 or p.max_replicas < 0:
            raise ValueError(f"{p.partition}: replicas must be >= 0")
        if p.min_replicas > p.max_replicas:
            raise ValueError(f"{p.partition}: min_replicas cannot be larger than max_replicas")
        if p.scale_up_step <= 0 or p.scale_down_step <= 0:
            raise ValueError(f"{p.partition}: scale steps must be > 0")
        if p.scale_down_cooldown < 0:
            raise ValueError(f"{p.partition}: scale_down_cooldown must be >= 0")
        if p.max_checkpoint_age_seconds < 0:
            raise ValueError(f"{p.partition}: max_checkpoint_age_seconds must be >= 0")


def main() -> None:
    cfg = Config()
    partition_cfgs = PartitionConfigLoader.load(cfg)
    validate_config(cfg, partition_cfgs)
    OperatorApp(cfg).run()


if __name__ == "__main__":
    main()
