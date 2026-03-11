#!/usr/bin/env python3
"""Phase 2 elastic scaler.

Milestone C + D implementation:
- Partition-aware independent scaling.
- Checkpoint-aware scale-down guard.

Phase A implementation:
- Introduce WorkerClass / NodeSet topology model.
- Load topology from a ConfigMap so operator logic is driven by declarative config.
- Keep backward compatibility with legacy single-partition env vars.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


@dataclass(frozen=True)
class WorkerClass:
    name: str
    description: str = ""
    image: str = ""
    resources: dict[str, Any] = field(default_factory=dict)
    node_selector: dict[str, str] = field(default_factory=dict)
    tolerations: list[dict[str, Any]] = field(default_factory=list)
    slurm_features: list[str] = field(default_factory=list)
    gres: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NodeSet:
    name: str
    worker_class: str
    partition: str
    worker_statefulset: str
    node_name_prefix: str
    service_name: str = ""
    min_replicas: int = 1
    max_replicas: int = 4
    scale_up_step: int = 1
    scale_down_step: int = 1
    scale_down_cooldown: int = 60
    checkpoint_path: str = ""
    max_checkpoint_age_seconds: int = 600


@dataclass(frozen=True)
class PartitionConfig:
    partition: str
    worker_statefulset: str
    node_name_prefix: str
    service_name: str
    worker_class: str
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
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
    policy_name: str = os.getenv("SCALING_POLICY", "checkpoint_aware_queue")
    checkpoint_guard_enabled: bool = os.getenv("CHECKPOINT_GUARD_ENABLED", "true").lower() == "true"
    topology_configmap: str = os.getenv("WORKER_TOPOLOGY_CONFIGMAP", "slurm-topology")
    topology_key: str = os.getenv("WORKER_TOPOLOGY_KEY", "topology.json")
    # Legacy single-partition fallback.
    default_partition: str = os.getenv("SLURM_PARTITION", "debug")
    default_worker_statefulset: str = os.getenv("WORKER_STATEFULSET", "slurm-worker")
    default_node_name_prefix: str = os.getenv("WORKER_NODE_PREFIX", os.getenv("WORKER_STATEFULSET", "slurm-worker"))
    default_service_name: str = os.getenv("WORKER_SERVICE", os.getenv("WORKER_STATEFULSET", "slurm-worker"))
    default_worker_class: str = os.getenv("WORKER_CLASS", "default")
    default_min_replicas: int = int(os.getenv("MIN_REPLICAS", "1"))
    default_max_replicas: int = int(os.getenv("MAX_REPLICAS", "4"))
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

    def try_run(self, args: Iterable[str]) -> tuple[int, str, str]:
        cmd = ["kubectl", *args]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    def pod_is_ready(self, pod_name: str) -> bool:
        rc, out, _ = self.try_run(
            [
                "-n",
                self.cfg.namespace,
                "get",
                "pod",
                pod_name,
                "-o",
                "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
            ]
        )
        return rc == 0 and out == "True"

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


class TopologyLoader:
    def __init__(self, client: KubectlClient, cfg: Config):
        self.client = client
        self.cfg = cfg

    def _load_topology_json(self) -> dict[str, Any] | None:
        if not self.cfg.topology_configmap:
            return None
        rc, out, err = self.client.try_run(
            [
                "-n",
                self.cfg.namespace,
                "get",
                "configmap",
                self.cfg.topology_configmap,
                "-o",
                f"jsonpath={{.data.{self.cfg.topology_key}}}",
            ]
        )
        if rc != 0 or not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"failed to parse topology configmap {self.cfg.topology_configmap}/{self.cfg.topology_key} as JSON"
            ) from exc

    def load(self) -> tuple[dict[str, WorkerClass], list[NodeSet], list[PartitionConfig]]:
        payload = self._load_topology_json()
        if not payload:
            worker_classes = {
                self.cfg.default_worker_class: WorkerClass(name=self.cfg.default_worker_class)
            }
            node_sets = [
                NodeSet(
                    name=self.cfg.default_worker_statefulset,
                    worker_class=self.cfg.default_worker_class,
                    partition=self.cfg.default_partition,
                    worker_statefulset=self.cfg.default_worker_statefulset,
                    node_name_prefix=self.cfg.default_node_name_prefix,
                    service_name=self.cfg.default_service_name,
                    min_replicas=self.cfg.default_min_replicas,
                    max_replicas=self.cfg.default_max_replicas,
                    scale_up_step=self.cfg.default_scale_up_step,
                    scale_down_step=self.cfg.default_scale_down_step,
                    scale_down_cooldown=self.cfg.default_scale_down_cooldown,
                    checkpoint_path=self.cfg.default_checkpoint_path,
                    max_checkpoint_age_seconds=self.cfg.default_max_checkpoint_age_seconds,
                )
            ]
            partitions = [
                PartitionConfig(
                    partition=ns.partition,
                    worker_statefulset=ns.worker_statefulset,
                    node_name_prefix=ns.node_name_prefix,
                    service_name=ns.service_name or ns.worker_statefulset,
                    worker_class=ns.worker_class,
                    min_replicas=ns.min_replicas,
                    max_replicas=ns.max_replicas,
                    scale_up_step=ns.scale_up_step,
                    scale_down_step=ns.scale_down_step,
                    scale_down_cooldown=ns.scale_down_cooldown,
                    checkpoint_path=ns.checkpoint_path,
                    max_checkpoint_age_seconds=ns.max_checkpoint_age_seconds,
                )
                for ns in node_sets
            ]
            return worker_classes, node_sets, partitions

        raw_worker_classes = payload.get("workerClasses", [])
        raw_node_sets = payload.get("nodeSets", [])
        if not isinstance(raw_worker_classes, list) or not isinstance(raw_node_sets, list):
            raise ValueError("topology.json must contain 'workerClasses' and 'nodeSets' arrays")

        worker_classes: dict[str, WorkerClass] = {}
        for item in raw_worker_classes:
            wc = WorkerClass(
                name=item["name"],
                description=item.get("description", ""),
                image=item.get("image", ""),
                resources=item.get("resources", {}),
                node_selector=item.get("nodeSelector", {}),
                tolerations=item.get("tolerations", []),
                slurm_features=item.get("slurmFeatures", []),
                gres=item.get("gres", []),
            )
            worker_classes[wc.name] = wc

        node_sets: list[NodeSet] = []
        partitions: list[PartitionConfig] = []
        for item in raw_node_sets:
            worker_class = item["workerClass"]
            if worker_class not in worker_classes:
                raise ValueError(f"nodeset {item.get('name','<unknown>')} references unknown workerClass {worker_class}")
            ns = NodeSet(
                name=item["name"],
                worker_class=worker_class,
                partition=item.get("partition", self.cfg.default_partition),
                worker_statefulset=item.get("statefulset", item["name"]),
                node_name_prefix=item.get("nodeNamePrefix", item.get("statefulset", item["name"])),
                service_name=item.get("serviceName", item.get("statefulset", item["name"])),
                min_replicas=int(item.get("minReplicas", 0)),
                max_replicas=int(item.get("maxReplicas", self.cfg.default_max_replicas)),
                scale_up_step=int(item.get("scaleUpStep", 1)),
                scale_down_step=int(item.get("scaleDownStep", 1)),
                scale_down_cooldown=int(item.get("scaleDownCooldownSeconds", 60)),
                checkpoint_path=item.get("checkpointPath", ""),
                max_checkpoint_age_seconds=int(item.get("maxCheckpointAgeSeconds", 600)),
            )
            node_sets.append(ns)
            partitions.append(
                PartitionConfig(
                    partition=ns.partition,
                    worker_statefulset=ns.worker_statefulset,
                    node_name_prefix=ns.node_name_prefix,
                    service_name=ns.service_name,
                    worker_class=ns.worker_class,
                    min_replicas=ns.min_replicas,
                    max_replicas=ns.max_replicas,
                    scale_up_step=ns.scale_up_step,
                    scale_down_step=ns.scale_down_step,
                    scale_down_cooldown=ns.scale_down_cooldown,
                    checkpoint_path=ns.checkpoint_path,
                    max_checkpoint_age_seconds=ns.max_checkpoint_age_seconds,
                )
            )
        return worker_classes, node_sets, partitions


class ClusterStateCollector:
    def __init__(self, client: KubectlClient):
        self.client = client

    def get_current_replicas(self, statefulset: str) -> int:
        output = self.client.run([
            "-n", self.client.cfg.namespace, "get", "statefulset", statefulset, "-o", "json"
        ])
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
            rf"sinfo -h -p {partition} -N -o '%T' | egrep -Ei 'ALLOCATED|MIXED|COMPLETING' | wc -l || true"
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

    def evaluate(self, partition_cfg: PartitionConfig, state: PartitionState, checkpoint_age_seconds: int | None) -> ScalingDecision:
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
                return ScalingDecision(state.current_replicas, "keep", "checkpoint_unknown_block_scale_down")
            if checkpoint_age_seconds > partition_cfg.max_checkpoint_age_seconds:
                return ScalingDecision(state.current_replicas, "keep", "checkpoint_stale_block_scale_down")

        return self._to_decision(state.current_replicas, candidate_target, "no_pending_jobs")

    @staticmethod
    def _to_decision(current: int, target: int, reason: str) -> ScalingDecision:
        if target > current:
            return ScalingDecision(target, "scale_up", reason)
        if target < current:
            return ScalingDecision(target, "scale_down", reason)
        return ScalingDecision(target, "keep", reason)


class StatefulSetActuator:
    def __init__(self, client: KubectlClient):
        self.client = client

    def patch_replicas(self, statefulset: str, replicas: int) -> None:
        self.client.run([
            "-n", self.client.cfg.namespace, "patch", "statefulset", statefulset,
            "--type=merge", "-p", json.dumps({"spec": {"replicas": replicas}}),
        ])


class OperatorApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.logger = JsonLogger()
        self.client = KubectlClient(cfg)
        self.collector = ClusterStateCollector(self.client)
        self.policy = CheckpointAwareQueuePolicy(cfg.checkpoint_guard_enabled)
        self.actuator = StatefulSetActuator(self.client)
        self.worker_classes, self.node_sets, self.partition_cfgs = TopologyLoader(self.client, cfg).load()
        self.last_scale_up_at: dict[str, float] = {
            f"{p.partition}:{p.worker_statefulset}": 0.0 for p in self.partition_cfgs
        }

    def _sync_slurm_node_states(self, partition_cfg: PartitionConfig) -> None:
        for i in range(partition_cfg.max_replicas):
            pod_name = f"{partition_cfg.worker_statefulset}-{i}"
            node_name = f"{partition_cfg.node_name_prefix}-{i}"
            if self.client.pod_is_ready(pod_name):
                self.client.exec_in_controller(
                    f"scontrol update NodeName={node_name} State=RESUME || true"
                )
            else:
                self.client.exec_in_controller(
                    f"scontrol update NodeName={node_name} State=DOWN Reason=autoscale || true"
                )

    def run(self) -> None:
        self.logger.emit(
            "startup",
            policy=self.cfg.policy_name,
            config=asdict(self.cfg),
            worker_classes=[asdict(wc) for wc in self.worker_classes.values()],
            node_sets=[asdict(ns) for ns in self.node_sets],
            partitions=[asdict(p) for p in self.partition_cfgs],
        )

        while True:
            for partition_cfg in self.partition_cfgs:
                key = f"{partition_cfg.partition}:{partition_cfg.worker_statefulset}"
                try:
                    state = self.collector.collect_partition_state(partition_cfg)
                    checkpoint_age = self.collector.get_checkpoint_age_seconds(partition_cfg.checkpoint_path)
                    decision = self.policy.evaluate(partition_cfg, state, checkpoint_age)

                    now = time.time()
                    cooldown_elapsed = now - self.last_scale_up_at[key]
                    cooldown_remaining = max(partition_cfg.scale_down_cooldown - int(cooldown_elapsed), 0)

                    self.logger.emit(
                        "loop_observation",
                        policy=self.cfg.policy_name,
                        partition=partition_cfg.partition,
                        worker_statefulset=partition_cfg.worker_statefulset,
                        worker_class=partition_cfg.worker_class,
                        state=asdict(state),
                        decision=asdict(decision),
                        checkpoint_age_seconds=checkpoint_age,
                        cooldown_remaining_seconds=cooldown_remaining,
                    )

                    if decision.action == "scale_up":
                        self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
                        self.last_scale_up_at[key] = now
                        self.logger.emit(
                            "scale_action",
                            policy=self.cfg.policy_name,
                            partition=partition_cfg.partition,
                            worker_statefulset=partition_cfg.worker_statefulset,
                            worker_class=partition_cfg.worker_class,
                            action="scale_up",
                            from_replicas=state.current_replicas,
                            to_replicas=decision.target_replicas,
                            reason=decision.reason,
                            pending_jobs=state.pending_jobs,
                            running_jobs=state.running_jobs,
                            busy_nodes=state.busy_nodes,
                        )
                    elif decision.action == "scale_down":
                        if cooldown_elapsed >= partition_cfg.scale_down_cooldown:
                            self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
                            self.logger.emit(
                                "scale_action",
                                policy=self.cfg.policy_name,
                                partition=partition_cfg.partition,
                                worker_statefulset=partition_cfg.worker_statefulset,
                                worker_class=partition_cfg.worker_class,
                                action="scale_down",
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
                                partition=partition_cfg.partition,
                                worker_statefulset=partition_cfg.worker_statefulset,
                                worker_class=partition_cfg.worker_class,
                                action="scale_down",
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
                            partition=partition_cfg.partition,
                            worker_statefulset=partition_cfg.worker_statefulset,
                            worker_class=partition_cfg.worker_class,
                            action="keep",
                            from_replicas=state.current_replicas,
                            to_replicas=decision.target_replicas,
                            reason=decision.reason,
                            checkpoint_age_seconds=checkpoint_age,
                            pending_jobs=state.pending_jobs,
                            running_jobs=state.running_jobs,
                            busy_nodes=state.busy_nodes,
                        )

                    self._sync_slurm_node_states(partition_cfg)
                except Exception as exc:  # noqa: BLE001
                    self.logger.emit(
                        "error",
                        level="ERROR",
                        partition=partition_cfg.partition,
                        worker_statefulset=partition_cfg.worker_statefulset,
                        message=str(exc),
                    )
            time.sleep(self.cfg.poll_interval)


def validate_config(cfg: Config, worker_classes: dict[str, WorkerClass], node_sets: list[NodeSet], partition_cfgs: list[PartitionConfig]) -> None:
    if cfg.poll_interval <= 0:
        raise ValueError("POLL_INTERVAL_SECONDS must be > 0")
    if not worker_classes:
        raise ValueError("at least one WorkerClass must be defined")
    if not node_sets:
        raise ValueError("at least one NodeSet must be defined")

    seen_sts: set[str] = set()
    for ns in node_sets:
        if ns.worker_statefulset in seen_sts:
            raise ValueError(f"duplicate worker_statefulset in NodeSets: {ns.worker_statefulset}")
        seen_sts.add(ns.worker_statefulset)
        if ns.worker_class not in worker_classes:
            raise ValueError(f"nodeset {ns.name} references unknown worker class {ns.worker_class}")

    for p in partition_cfgs:
        if p.min_replicas < 0 or p.max_replicas < 0:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: replicas must be >= 0")
        if p.min_replicas > p.max_replicas:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: min_replicas cannot be larger than max_replicas")
        if p.scale_up_step <= 0 or p.scale_down_step <= 0:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: scale steps must be > 0")
        if p.scale_down_cooldown < 0:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: scale_down_cooldown must be >= 0")
        if p.max_checkpoint_age_seconds < 0:
            raise ValueError(f"{p.partition}/{p.worker_statefulset}: max_checkpoint_age_seconds must be >= 0")


def main() -> None:
    cfg = Config()
    client = KubectlClient(cfg)
    worker_classes, node_sets, partition_cfgs = TopologyLoader(client, cfg).load()
    validate_config(cfg, worker_classes, node_sets, partition_cfgs)
    OperatorApp(cfg).run()


if __name__ == "__main__":
    main()
