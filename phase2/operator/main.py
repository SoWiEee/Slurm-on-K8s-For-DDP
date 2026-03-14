#!/usr/bin/env python3
"""Phase 2 elastic scaler.

Phase A implementation:
- Introduce WorkerClass / NodeSet topology model.
- Load topology from a ConfigMap so operator logic is driven by declarative config.
- Keep backward compatibility with legacy single-partition env vars.

Phase B implementation:
- Parse basic Slurm queue demand from pending/running jobs.
- Extract nodes / cpus / gres / constraint.
- Match each job to the best NodeSet.
- Scale the corresponding StatefulSet based on demand instead of a single global pending-job count.

Phase C implementation:
- Upgrade autoscaling into a pool-aware FSM.
- Track K8s readiness + Slurm registration/health per NodeSet.
- Gate scale down until pools are steady, and emit explicit FSM transitions.
"""

from __future__ import annotations

import json
import os
import re
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
class JobDemand:
    job_id: str
    state: str
    partition: str
    nodes: int
    cpus_total: int
    cpus_per_node: int
    gres_types: tuple[str, ...]
    constraint_features: tuple[str, ...]
    allocated_nodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PartitionState:
    partition: str
    worker_statefulset: str
    current_replicas: int
    ready_replicas: int
    updated_replicas: int
    available_replicas: int
    slurm_ready_nodes: int
    slurm_down_nodes: int
    pending_jobs: int
    pending_nodes: int
    running_jobs: int
    busy_nodes: int


@dataclass(frozen=True)
class ScalingDecision:
    target_replicas: int
    action: str  # scale_up | scale_down | keep
    reason: str


@dataclass(frozen=True)
class PoolFSM:
    phase: str
    desired_replicas: int
    stable_replicas: int
    reasons: tuple[str, ...] = ()


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


def parse_cpu_quantity(value: Any) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("m"):
        try:
            milli = int(s[:-1])
            return max(1, milli // 1000)
        except ValueError:
            return None
    try:
        return int(float(s))
    except ValueError:
        return None


def normalize_gres_token(token: str) -> set[str]:
    s = token.strip()
    if not s or s in {"(null)", "N/A", "none"}:
        return set()
    if s.startswith("gres/"):
        s = s.split("/", 1)[1]
    if "=" in s:
        s = s.split("=", 1)[0]
    parts = [p for p in s.split(":") if p]
    if not parts:
        return set()
    if parts[0] != "gpu":
        return {parts[0]}
    out = {"gpu"}
    if len(parts) >= 2 and not parts[1].isdigit():
        out.add(f"gpu:{parts[1]}")
    return out


def extract_gres_types(*values: str) -> tuple[str, ...]:
    types: set[str] = set()
    for value in values:
        if not value or value in {"(null)", "N/A"}:
            continue
        for token in value.split(","):
            types.update(normalize_gres_token(token))
    return tuple(sorted(types))


def parse_constraint_features(value: str) -> tuple[str, ...]:
    if not value or value in {"(null)", "N/A", "none"}:
        return ()
    tokens = [t for t in re.split(r"[,&|()\[\]\s]+", value) if t and t.lower() != "null"]
    return tuple(sorted(set(tokens)))


def parse_nodes_count(value: str) -> int:
    if not value or value in {"(null)", "N/A"}:
        return 1
    first = value.split(",", 1)[0].strip()
    m = re.match(r"^(\d+)(?:-(\d+))?$", first)
    if not m:
        try:
            return int(first)
        except ValueError:
            return 1
    if m.group(2):
        return int(m.group(2))
    return int(m.group(1))


def parse_nodelist_prefixes(value: str) -> tuple[str, ...]:
    if not value or value in {"(null)", "N/A"}:
        return ()
    prefixes: set[str] = set()
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        prefix = token.split("[", 1)[0]
        prefix = prefix.split("-", 1)[0] if prefix.startswith("/") else prefix
        if prefix:
            prefixes.add(prefix)
    return tuple(sorted(prefixes))


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
                "-n", self.cfg.namespace,
                "get", "pod", pod_name,
                "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}",
            ]
        )
        return rc == 0 and out == "True"

    def get_statefulset_status(self, statefulset: str) -> dict[str, int]:
        payload = json.loads(self.run(["-n", self.cfg.namespace, "get", "statefulset", statefulset, "-o", "json"]))
        spec = payload.get("spec", {})
        status = payload.get("status", {})
        return {
            "spec_replicas": int(spec.get("replicas", 0) or 0),
            "ready_replicas": int(status.get("readyReplicas", 0) or 0),
            "updated_replicas": int(status.get("updatedReplicas", 0) or 0),
            "available_replicas": int(status.get("availableReplicas", 0) or 0),
        }

    def exec_in_controller(self, command: str) -> str:
        return self.run(
            [
                "-n", self.cfg.namespace,
                "exec", f"pod/{self.cfg.controller_pod}",
                "--", "bash", "-lc", command,
            ]
        )

    def controller_slurm_node_info(self, node_name: str) -> dict[str, str]:
        rc, out, _ = self.try_run(
            [
                "-n", self.cfg.namespace,
                "exec", f"pod/{self.cfg.controller_pod}",
                "--", "bash", "-lc", f"scontrol show node {node_name} -o 2>/dev/null || true",
            ]
        )
        if rc != 0 or not out:
            return {}
        pairs = {}
        for part in out.split():
            if "=" in part:
                k, v = part.split("=", 1)
                pairs[k] = v
        return pairs


class TopologyLoader:
    def __init__(self, client: KubectlClient, cfg: Config):
        self.client = client
        self.cfg = cfg

    def _load_topology_json(self) -> dict[str, Any] | None:
        if not self.cfg.topology_configmap:
            return None
        rc, out, _ = self.client.try_run(
            [
                "-n", self.cfg.namespace,
                "get", "configmap", self.cfg.topology_configmap,
                "-o", f"jsonpath={{.data.{self.cfg.topology_key}}}",
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
            worker_classes = {self.cfg.default_worker_class: WorkerClass(name=self.cfg.default_worker_class)}
            node_sets = [NodeSet(
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
            )]
            parts = [PartitionConfig(
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
            ) for ns in node_sets]
            return worker_classes, node_sets, parts

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
        partition_cfgs: list[PartitionConfig] = []
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
            partition_cfgs.append(PartitionConfig(
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
            ))
        return worker_classes, node_sets, partition_cfgs


class ClusterStateCollector:
    def __init__(self, client: KubectlClient, worker_classes: dict[str, WorkerClass], partition_cfgs: list[PartitionConfig]):
        self.client = client
        self.worker_classes = worker_classes
        self.partition_cfgs = partition_cfgs

    def get_checkpoint_age_seconds(self, checkpoint_path: str) -> int | None:
        if not checkpoint_path:
            return None
        command = (
            f"if [ -f '{checkpoint_path}' ]; then now=$(date +%s); mtime=$(stat -c %Y '{checkpoint_path}'); "
            "echo $((now - mtime)); else echo -1; fi"
        )
        output = self.client.exec_in_controller(command)
        age = int(output or "-1")
        return None if age < 0 else age

    def get_busy_nodes(self, node_name_prefix: str) -> int:
        output = self.client.exec_in_controller(
            rf"sinfo -h -N -o '%N|%T' | awk -F'|' '$1 ~ /^{re.escape(node_name_prefix)}-/ && $2 ~ /(ALLOCATED|MIXED|COMPLETING|allocated|mixed|completing)/ {{count++}} END {{print count+0}}' || true"
        )
        return int(output or "0")

    def _fetch_job_lines(self) -> list[str]:
        job_ids = self.client.exec_in_controller("squeue -h -t PENDING,RUNNING -o '%i' | paste -sd, -")
        if not job_ids:
            return []
        output = self.client.exec_in_controller(f"scontrol show job -o {job_ids}")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _parse_job_line(self, line: str) -> JobDemand | None:
        pairs = dict(part.split("=", 1) for part in line.split() if "=" in part)
        state = pairs.get("JobState", "")
        partition = pairs.get("Partition", "")
        if state not in {"PENDING", "RUNNING"} or not partition:
            return None
        nodes = parse_nodes_count(pairs.get("NumNodes", "1"))
        cpus_total = int(pairs.get("NumCPUs", "0") or "0")
        cpus_per_node = max(1, cpus_total // max(1, nodes)) if cpus_total > 0 else 1
        if "CPUs/Task" in pairs:
            try:
                cpus_per_task = int(pairs.get("CPUs/Task", "0") or "0")
                if cpus_per_task > 0:
                    cpus_per_node = max(cpus_per_node, cpus_per_task)
            except ValueError:
                pass
        return JobDemand(
            job_id=pairs.get("JobId", "unknown"),
            state=state,
            partition=partition,
            nodes=nodes,
            cpus_total=cpus_total,
            cpus_per_node=cpus_per_node,
            gres_types=extract_gres_types(
                pairs.get("Gres", ""),
                pairs.get("TresPerNode", ""),
                pairs.get("ReqTRES", ""),
            ),
            constraint_features=parse_constraint_features(pairs.get("Features", "") or pairs.get("Constraint", "")),
            allocated_nodes=parse_nodelist_prefixes(pairs.get("NodeList", "")),
        )

    def get_job_demands(self) -> list[JobDemand]:
        demands: list[JobDemand] = []
        for line in self._fetch_job_lines():
            demand = self._parse_job_line(line)
            if demand is not None:
                demands.append(demand)
        return demands


class NodeSetMatcher:
    def __init__(self, worker_classes: dict[str, WorkerClass], partition_cfgs: list[PartitionConfig]):
        self.worker_classes = worker_classes
        self.partition_cfgs = partition_cfgs

    def _worker_cpu_capacity(self, wc: WorkerClass) -> int | None:
        resources = wc.resources or {}
        for root in ("limits", "requests"):
            if isinstance(resources.get(root), dict):
                cpu = parse_cpu_quantity(resources[root].get("cpu"))
                if cpu is not None:
                    return cpu
        return None

    def _score(self, job: JobDemand, partition_cfg: PartitionConfig) -> int | None:
        if partition_cfg.partition != job.partition:
            return None
        wc = self.worker_classes[partition_cfg.worker_class]
        wc_features = set(wc.slurm_features)
        job_features = set(job.constraint_features)
        if job_features and not job_features.issubset(wc_features):
            return None
        wc_gres = set(extract_gres_types(",".join(wc.gres)))
        job_gres = set(job.gres_types)
        if job_gres and not job_gres.issubset(wc_gres):
            return None
        cpu_cap = self._worker_cpu_capacity(wc)
        if cpu_cap is not None and job.cpus_per_node > cpu_cap:
            return None

        score = 0
        score += len(job_features & wc_features) * 10
        score += len(job_gres & wc_gres) * 20
        if cpu_cap is not None:
            score += max(0, 10 - abs(cpu_cap - job.cpus_per_node))
        score += max(0, 10 - partition_cfg.min_replicas)
        return score

    def assign(self, job: JobDemand) -> PartitionConfig | None:
        if job.allocated_nodes:
            for p in self.partition_cfgs:
                if p.node_name_prefix in job.allocated_nodes:
                    return p
        ranked: list[tuple[int, PartitionConfig]] = []
        for p in self.partition_cfgs:
            score = self._score(job, p)
            if score is not None:
                ranked.append((score, p))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (-item[0], item[1].worker_statefulset))
        return ranked[0][1]

    def assign_jobs(self, jobs: list[JobDemand]) -> tuple[dict[str, list[JobDemand]], list[JobDemand]]:
        assigned: dict[str, list[JobDemand]] = {p.worker_statefulset: [] for p in self.partition_cfgs}
        unmatched: list[JobDemand] = []
        for job in jobs:
            match = self.assign(job)
            if match is None:
                unmatched.append(job)
            else:
                assigned[match.worker_statefulset].append(job)
        return assigned, unmatched


class CheckpointAwareQueuePolicy:
    def __init__(self, guard_enabled: bool):
        self.guard_enabled = guard_enabled

    def evaluate(self, partition_cfg: PartitionConfig, state: PartitionState, checkpoint_age_seconds: int | None) -> ScalingDecision:
        if state.pending_nodes > 0:
            target = clamp(
                max(state.current_replicas + partition_cfg.scale_up_step, state.busy_nodes + state.pending_nodes),
                partition_cfg.min_replicas,
                partition_cfg.max_replicas,
            )
            return self._to_decision(state.current_replicas, target, "pending_demand")

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


class PoolAwareFSM:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def evaluate(self, partition_cfg: PartitionConfig, state: PartitionState, decision: ScalingDecision) -> PoolFSM:
        reasons: list[str] = []
        desired = decision.target_replicas
        stable = min(state.current_replicas, state.ready_replicas, state.slurm_ready_nodes)

        if decision.action == "scale_up":
            reasons.append(decision.reason)
            return PoolFSM("scale_up_requested", desired_replicas=desired, stable_replicas=stable, reasons=tuple(reasons))

        if state.updated_replicas < state.current_replicas or state.ready_replicas < state.current_replicas:
            reasons.append("waiting_k8s_readiness")
            return PoolFSM("waiting_k8s_ready", desired_replicas=desired, stable_replicas=stable, reasons=tuple(reasons))

        if state.slurm_ready_nodes < state.current_replicas:
            reasons.append("waiting_slurm_registration")
            return PoolFSM("waiting_slurm_ready", desired_replicas=desired, stable_replicas=stable, reasons=tuple(reasons))

        if state.pending_nodes > 0 and stable < desired:
            reasons.append("capacity_not_ready_for_pending")
            return PoolFSM("warming_for_pending_jobs", desired_replicas=desired, stable_replicas=stable, reasons=tuple(reasons))

        if decision.action == "scale_down":
            reasons.append(decision.reason)
            return PoolFSM("scale_down_candidate", desired_replicas=desired, stable_replicas=stable, reasons=tuple(reasons))

        reasons.append("steady")
        return PoolFSM("steady", desired_replicas=desired, stable_replicas=stable, reasons=tuple(reasons))


class StatefulSetActuator:
    def __init__(self, client: KubectlClient):
        self.client = client

    def patch_replicas(self, statefulset: str, replicas: int) -> None:
        self.client.run([
            "-n", self.client.cfg.namespace,
            "patch", "statefulset", statefulset,
            "--type=merge", "-p", json.dumps({"spec": {"replicas": replicas}}),
        ])


class OperatorApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.logger = JsonLogger()
        self.client = KubectlClient(cfg)
        self.worker_classes, self.node_sets, self.partition_cfgs = TopologyLoader(self.client, cfg).load()
        self.collector = ClusterStateCollector(self.client, self.worker_classes, self.partition_cfgs)
        self.matcher = NodeSetMatcher(self.worker_classes, self.partition_cfgs)
        self.policy = CheckpointAwareQueuePolicy(cfg.checkpoint_guard_enabled)
        self.fsm = PoolAwareFSM(cfg)
        self.actuator = StatefulSetActuator(self.client)
        self.last_scale_up_at: dict[str, float] = {f"{p.partition}:{p.worker_statefulset}": 0.0 for p in self.partition_cfgs}
        self.last_phase: dict[str, str] = {}

    def _sync_slurm_node_states(self, partition_cfg: PartitionConfig) -> None:
        for i in range(partition_cfg.max_replicas):
            pod_name = f"{partition_cfg.worker_statefulset}-{i}"
            node_name = f"{partition_cfg.node_name_prefix}-{i}"
            if self.client.pod_is_ready(pod_name):
                self.client.exec_in_controller(f"scontrol update NodeName={node_name} State=RESUME || true")
            else:
                self.client.exec_in_controller(f"scontrol update NodeName={node_name} State=DOWN Reason=autoscale || true")

    def _build_states(self) -> tuple[dict[str, PartitionState], list[JobDemand]]:
        all_jobs = self.collector.get_job_demands()
        assigned, unmatched = self.matcher.assign_jobs(all_jobs)
        states: dict[str, PartitionState] = {}
        for p in self.partition_cfgs:
            jobs = assigned.get(p.worker_statefulset, [])
            pending_jobs = [j for j in jobs if j.state == "PENDING"]
            running_jobs = [j for j in jobs if j.state == "RUNNING"]

            sts_status = self.client.get_statefulset_status(p.worker_statefulset)
            slurm_ready_nodes = 0
            slurm_down_nodes = 0
            for i in range(p.max_replicas):
                info = self.client.controller_slurm_node_info(f"{p.node_name_prefix}-{i}")
                if not info:
                    continue
                state = info.get("State", "")
                reason = info.get("Reason", "")
                if "DOWN" in state or reason in {"scaledown", "autoscale"}:
                    slurm_down_nodes += 1
                elif "IDLE" in state or "ALLOCATED" in state or "MIXED" in state or "COMPLETING" in state:
                    slurm_ready_nodes += 1

            states[p.worker_statefulset] = PartitionState(
                partition=p.partition,
                worker_statefulset=p.worker_statefulset,
                current_replicas=sts_status["spec_replicas"],
                ready_replicas=sts_status["ready_replicas"],
                updated_replicas=sts_status["updated_replicas"],
                available_replicas=sts_status["available_replicas"],
                slurm_ready_nodes=slurm_ready_nodes,
                slurm_down_nodes=slurm_down_nodes,
                pending_jobs=len(pending_jobs),
                pending_nodes=sum(j.nodes for j in pending_jobs),
                running_jobs=len(running_jobs),
                busy_nodes=self.collector.get_busy_nodes(p.node_name_prefix),
            )
        return states, unmatched

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
            try:
                states, unmatched = self._build_states()
                if unmatched:
                    self.logger.emit("unmatched_jobs", level="WARNING", jobs=[asdict(j) for j in unmatched])
            except Exception as exc:
                self.logger.emit("error", level="ERROR", message=f"failed to build states: {exc}")
                time.sleep(self.cfg.poll_interval)
                continue

            for partition_cfg in self.partition_cfgs:
                key = f"{partition_cfg.partition}:{partition_cfg.worker_statefulset}"
                try:
                    state = states[partition_cfg.worker_statefulset]
                    checkpoint_age = self.collector.get_checkpoint_age_seconds(partition_cfg.checkpoint_path)
                    decision = self.policy.evaluate(partition_cfg, state, checkpoint_age)
                    fsm_state = self.fsm.evaluate(partition_cfg, state, decision)
                    now = time.time()
                    cooldown_elapsed = now - self.last_scale_up_at[key]
                    cooldown_remaining = max(partition_cfg.scale_down_cooldown - int(cooldown_elapsed), 0)

                    prev_phase = self.last_phase.get(key)
                    if prev_phase != fsm_state.phase:
                        self.logger.emit(
                            "fsm_transition",
                            partition=partition_cfg.partition,
                            worker_statefulset=partition_cfg.worker_statefulset,
                            worker_class=partition_cfg.worker_class,
                            previous_phase=prev_phase or "unknown",
                            new_phase=fsm_state.phase,
                            reasons=list(fsm_state.reasons),
                            stable_replicas=fsm_state.stable_replicas,
                            desired_replicas=fsm_state.desired_replicas,
                        )
                        self.last_phase[key] = fsm_state.phase

                    self.logger.emit(
                        "loop_observation",
                        policy=self.cfg.policy_name,
                        partition=partition_cfg.partition,
                        worker_statefulset=partition_cfg.worker_statefulset,
                        worker_class=partition_cfg.worker_class,
                        state=asdict(state),
                        decision=asdict(decision),
                        fsm=asdict(fsm_state),
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
                            pending_nodes=state.pending_nodes,
                            running_jobs=state.running_jobs,
                            busy_nodes=state.busy_nodes,
                        )
                    elif decision.action == "scale_down":
                        if fsm_state.phase == "scale_down_candidate" and cooldown_elapsed >= partition_cfg.scale_down_cooldown and state.slurm_ready_nodes >= min(decision.target_replicas, state.current_replicas):
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
                                pending_nodes=state.pending_nodes,
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
                                reason="fsm_or_cooldown_gate",
                                cooldown_remaining_seconds=cooldown_remaining,
                                pending_jobs=state.pending_jobs,
                                pending_nodes=state.pending_nodes,
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
                            pending_nodes=state.pending_nodes,
                            running_jobs=state.running_jobs,
                            busy_nodes=state.busy_nodes,
                        )

                    self._sync_slurm_node_states(partition_cfg)
                except Exception as exc:
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
