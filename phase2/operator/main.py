#!/usr/bin/env python3
"""Phase 2 elastic scaler.

Simple, readable control loop:
1. Read slurm queue/node states by exec-ing commands in slurm-controller.
2. Calculate desired worker replicas.
3. Patch slurm-worker StatefulSet replicas.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Iterable


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


def run_kubectl(args: Iterable[str]) -> str:
    cmd = ["kubectl", *args]
    result = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def exec_in_controller(cfg: Config, command: str) -> str:
    return run_kubectl([
        "-n",
        cfg.namespace,
        "exec",
        f"pod/{cfg.controller_pod}",
        "--",
        "bash",
        "-lc",
        command,
    ])


def get_pending_jobs(cfg: Config) -> int:
    output = exec_in_controller(cfg, "squeue -h -t PENDING | wc -l")
    return int(output or "0")


def get_busy_nodes(cfg: Config) -> int:
    # ALLOCATED/MIXED/COMPLETING are considered busy.
    output = exec_in_controller(
        cfg,
        r"sinfo -h -N -o '%T' | egrep -E 'ALLOCATED|MIXED|COMPLETING' | wc -l || true",
    )
    return int(output or "0")


def get_current_replicas(cfg: Config) -> int:
    output = run_kubectl(
        [
            "-n",
            cfg.namespace,
            "get",
            "statefulset",
            cfg.worker_statefulset,
            "-o",
            "json",
        ]
    )
    payload = json.loads(output)
    return int(payload.get("spec", {}).get("replicas", 0))


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def patch_replicas(cfg: Config, replicas: int) -> None:
    run_kubectl(
        [
            "-n",
            cfg.namespace,
            "patch",
            "statefulset",
            cfg.worker_statefulset,
            "--type=merge",
            "-p",
            json.dumps({"spec": {"replicas": replicas}}),
        ]
    )


def desired_replicas(cfg: Config, current: int, pending_jobs: int, busy_nodes: int) -> int:
    if pending_jobs > 0:
        return clamp(current + cfg.scale_up_step, cfg.min_replicas, cfg.max_replicas)

    safe_floor = max(cfg.min_replicas, busy_nodes)
    return clamp(current - cfg.scale_down_step, safe_floor, cfg.max_replicas)


def validate_config(cfg: Config) -> None:
    if cfg.min_replicas < 0 or cfg.max_replicas < 0:
        raise ValueError("replicas must be >= 0")
    if cfg.min_replicas > cfg.max_replicas:
        raise ValueError("MIN_REPLICAS cannot be larger than MAX_REPLICAS")


def main() -> None:
    cfg = Config()
    validate_config(cfg)
    print(f"[phase2-operator] start with config: {cfg}")

    last_scale_up_at = 0.0
    while True:
        try:
            current = get_current_replicas(cfg)
            pending = get_pending_jobs(cfg)
            busy = get_busy_nodes(cfg)
            target = desired_replicas(cfg, current, pending, busy)

            now = time.time()
            if target > current:
                patch_replicas(cfg, target)
                last_scale_up_at = now
                print(
                    f"[phase2-operator] scale up {current} -> {target}; "
                    f"pending={pending}, busy={busy}"
                )
            elif target < current:
                cooldown_elapsed = now - last_scale_up_at
                if cooldown_elapsed >= cfg.scale_down_cooldown:
                    patch_replicas(cfg, target)
                    print(
                        f"[phase2-operator] scale down {current} -> {target}; "
                        f"pending={pending}, busy={busy}"
                    )
                else:
                    print(
                        "[phase2-operator] skip scale down due to cooldown; "
                        f"remaining={cfg.scale_down_cooldown - int(cooldown_elapsed)}s"
                    )
            else:
                print(
                    f"[phase2-operator] keep replicas={current}; "
                    f"pending={pending}, busy={busy}"
                )
        except Exception as exc:  # noqa: BLE001
            print(f"[phase2-operator] loop error: {exc}")

        time.sleep(cfg.poll_interval)


if __name__ == "__main__":
    main()
