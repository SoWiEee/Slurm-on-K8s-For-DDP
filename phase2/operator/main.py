#!/usr/bin/env python3
"""Phase 2 elastic scaler.

Multi-pool updates:
- Pool-aware scaling within the same Slurm partition.
- Pool matching by requested Features / GRES and by running node prefix.
- Best-effort slurmctld reconfigure + node state sync for dynamic pools.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from dataclasses import asdict, dataclass, field
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
                "jsonpath={.status.conditions[?(@.type==\'Ready\')].status}",
            ]
        )
        return rc == 0 and out == "True"

    # Backward-compatible alias for older call sites and previously built images.
    def pod_ready(self, pod_name: str) -> bool:
        return self.pod_is_ready(pod_name)

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
                    fallback=True,
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
                    max_replicas=int(item.get("max_replicas", cfg.default_max_replicas)),
                    scale_up_step=int(item.get("scale_up_step", 1)),
                    scale_down_step=int(item.get("scale_down_step", 1)),
                    scale_down_cooldown=int(item.get("scale_down_cooldown", 60)),
                    checkpoint_path=item.get("checkpoint_path", ""),
                    max_checkpoint_age_seconds=int(item.get("max_checkpoint_age_seconds", 600)),
                    match_features=tuple(item.get("match_features", [])),
                    match_gres=tuple(item.get("match_gres", [])),
                    fallback=bool(item.get("fallback", False)),
                )
            )
        return partitions


class ClusterStateCollector:
    def __init__(self, client: KubectlClient, partition_cfgs: list[PartitionConfig]):
        self.client = client
        self.partition_cfgs = partition_cfgs
        self.pool_order = list(partition_cfgs)

    @staticmethod
    def _parse_kv_line(line: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for token in line.split():
            if "=" not in token:
                continue
            k, v = token.split("=", 1)
            fields[k] = v
        return fields

    @staticmethod
    def _csv_set(value: str) -> set[str]:
        if not value or value == "(null)" or value == "N/A":
            return set()
        return {part for part in value.split(",") if part and part != "(null)"}

    @staticmethod
    def _gres_match(value: str, needles: tuple[str, ...]) -> bool:
        if not value or value == "(null)":
            return False
        return any(needle in value for needle in needles)

    def _classify_job(self, fields: dict[str, str]) -> PartitionConfig | None:
        node_list = fields.get("NodeList", "")
        if node_list and node_list != "(null)":
            for pool in self.pool_order:
                if node_list.startswith(pool.worker_statefulset):
                    return pool

        features = set()
        for key in ("Features", "Feature", "Constraints"):
            features |= self._csv_set(fields.get(key, ""))

        gres_blob = " ".join(
            fields.get(key, "") for key in ("TresPerNode", "TresPerJob", "TresBind", "TRES")
        )

        for pool in self.pool_order:
            if pool.match_features and any(feature in features for feature in pool.match_features):
                return pool
            if pool.match_gres and self._gres_match(gres_blob, pool.match_gres):
                return pool

        for pool in self.pool_order:
            if pool.fallback:
                return pool
        return None

    def _jobs_by_pool_and_state(self, partition: str) -> dict[str, dict[str, list[dict[str, str]]]]:
        """Fetch all PENDING and RUNNING jobs for a partition in a single squeue call.

        Returns {worker_statefulset: {"PENDING": [...], "RUNNING": [...]}}.
        Fields per job: NodeList, Features, TresPerNode — enough for pool classification.
        """
        output = self.client.exec_in_controller(
            f"squeue -h -p {partition} -t PENDING,RUNNING -o '%i|%T|%N|%f|%b' || true"
        )
        result: dict[str, dict[str, list[dict[str, str]]]] = {
            p.worker_statefulset: {"PENDING": [], "RUNNING": []}
            for p in self.partition_cfgs
            if p.partition == partition
        }
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 4)
            if len(parts) < 5:
                continue
            _jobid, state, nodelist, features, tres_per_node = parts
            fields = {
                "NodeList": nodelist,
                "Features": features,
                "TresPerNode": tres_per_node,
            }
            pool = self._classify_job(fields)
            if pool is None:
                continue
            bucket = result.setdefault(pool.worker_statefulset, {"PENDING": [], "RUNNING": []})
            if state in ("PENDING", "RUNNING"):
                bucket[state].append(fields)
        return result

    def get_current_replicas(self, statefulset: str) -> int:
        output = self.client.run(
            ["-n", self.client.cfg.namespace, "get", "statefulset", statefulset, "-o", "json"]
        )
        payload = json.loads(output)
        return int(payload.get("spec", {}).get("replicas", 0))

    def get_busy_nodes(self, partition_cfg: PartitionConfig) -> int:
        prefix = partition_cfg.worker_statefulset
        output = self.client.exec_in_controller(
            rf"sinfo -h -p {partition_cfg.partition} -N -o '%N %T' | awk '$1 ~ /^{prefix}(-|$)/ && $2 ~ /ALLOCATED|MIXED|COMPLETING/ {{count++}} END {{print count+0}}'"
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
        return None if age < 0 else age

    def collect_partition_state(self, p: PartitionConfig) -> PartitionState:
        jobs = self._jobs_by_pool_and_state(p.partition)
        pool_jobs = jobs.get(p.worker_statefulset, {"PENDING": [], "RUNNING": []})
        return PartitionState(
            partition=p.partition,
            worker_statefulset=p.worker_statefulset,
            current_replicas=self.get_current_replicas(p.worker_statefulset),
            pending_jobs=len(pool_jobs["PENDING"]),
            running_jobs=len(pool_jobs["RUNNING"]),
            busy_nodes=self.get_busy_nodes(p),
        )

    def collect_all_partition_states(self) -> dict[str, PartitionState]:
        """Collect state for all pools with minimal squeue calls.

        Jobs are fetched once per unique partition name, so pools that share a
        partition (e.g. all three pools using 'debug') only trigger one squeue
        exec instead of one per pool.
        """
        jobs_by_partition: dict[str, dict[str, dict[str, list[dict[str, str]]]]] = {
            partition: self._jobs_by_pool_and_state(partition)
            for partition in {p.partition for p in self.partition_cfgs}
        }
        return {
            p.worker_statefulset: PartitionState(
                partition=p.partition,
                worker_statefulset=p.worker_statefulset,
                current_replicas=self.get_current_replicas(p.worker_statefulset),
                pending_jobs=len(
                    jobs_by_partition[p.partition].get(p.worker_statefulset, {}).get("PENDING", [])
                ),
                running_jobs=len(
                    jobs_by_partition[p.partition].get(p.worker_statefulset, {}).get("RUNNING", [])
                ),
                busy_nodes=self.get_busy_nodes(p),
            )
            for p in self.partition_cfgs
        }


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
        self.partition_cfgs = PartitionConfigLoader.load(cfg)
        self.collector = ClusterStateCollector(self.client, self.partition_cfgs)
        self.policy = CheckpointAwareQueuePolicy(cfg.checkpoint_guard_enabled)
        self.actuator = StatefulSetActuator(self.client)
        self.last_scale_up_at: dict[str, float] = {p.worker_statefulset: 0.0 for p in self.partition_cfgs}

    def _reconfigure_slurm(self) -> None:
        """Avoid periodic reconfigure storms.

        The static slurm.conf already contains all potential nodes. Calling
        `scontrol reconfigure` on every polling loop forces slurmctld to resolve
        every configured NodeAddr, including pods for scaled-to-zero replicas.
        In Kubernetes those pod FQDNs do not exist until the pod exists, so the
        controller can block on repeated DNS failures and make `sinfo`/`squeue`
        time out.

        Keep the hook for future targeted use, but do nothing in the steady-state
        operator loop.
        """
        return None

    def _sync_slurm_node_states(self, partition_cfg: PartitionConfig) -> None:
        """Best-effort node sync without touching non-existent pods.

        Slurm learns scaled-up nodes from slurmd registration automatically, and
        scaled-down nodes naturally become non-responding. Avoid probing every
        configured ordinal because most of them are intentionally absent when the
        pool is scaled down.
        """
        return None

    def run(self) -> None:
        self.logger.emit(
            "startup",
            policy=self.cfg.policy_name,
            config=asdict(self.cfg),
            partitions=[asdict(p) for p in self.partition_cfgs],
        )
        while True:
            all_states = self.collector.collect_all_partition_states()
            for partition_cfg in self.partition_cfgs:
                key = partition_cfg.worker_statefulset
                try:
                    state = all_states[key]
                    checkpoint_age = self.collector.get_checkpoint_age_seconds(partition_cfg.checkpoint_path)
                    decision = self.policy.evaluate(partition_cfg, state, checkpoint_age)

                    now = time.time()
                    cooldown_elapsed = now - self.last_scale_up_at[key]
                    cooldown_remaining = max(partition_cfg.scale_down_cooldown - int(cooldown_elapsed), 0)

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
                        self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
                        self.last_scale_up_at[key] = now
                        self.logger.emit(
                            "scale_action",
                            policy=self.cfg.policy_name,
                            partition=partition_cfg.partition,
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
                            self.actuator.patch_replicas(partition_cfg.worker_statefulset, decision.target_replicas)
                            self.logger.emit(
                                "scale_action",
                                policy=self.cfg.policy_name,
                                partition=partition_cfg.partition,
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
                                partition=partition_cfg.partition,
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
                            partition=partition_cfg.partition,
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
                except Exception as exc:  # noqa: BLE001
                    self.logger.emit("error", level="ERROR", partition=partition_cfg.partition, statefulset=key, message=str(exc))
            pathlib.Path("/tmp/operator-alive").touch()
            time.sleep(self.cfg.poll_interval)


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
