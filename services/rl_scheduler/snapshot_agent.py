"""M11 Phase C-3: snapshot agent — periodically polls slurmrestd and
pushes a cluster snapshot to ``serve.py``'s POST /snapshot.

Modes:
- ``--source slurmrestd`` — live cluster (re-uses operator's slurmrestd
  client; needs SLURMRESTD_URL + token env).
- ``--source sim``  — drives the offline simulator one step and
  publishes its state.  Useful for end-to-end testing without Slurm.

Run:
    .venv-m11/bin/python -m services.rl_scheduler.snapshot_agent \\
        --serve-url http://127.0.0.1:8002 --source sim --interval 2
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from typing import Any

from sim.loader import MPS_PER_GPU


def build_sim_snapshot(*, n_nodes: int = 4, gpus_per_node: int = 4,
                        n_jobs: int = 20, seed: int = 42) -> dict[str, Any]:
    """Deterministic-seeded sim snapshot for local integration testing."""
    from sim.loader import generate_by_family
    jobs = generate_by_family("philly", n_jobs=n_jobs, seed=seed)
    # All pending at t=0 view
    pending = [
        {
            "job_id": j.job_id,
            "mps_req": j.mps_req,
            "gpu_count": j.gpu_count,
            "gpu_type": j.gpu_type,
            "runtime": j.runtime,
            "submit_ts": j.submit_ts,
            "can_fit": True,
        }
        for j in jobs[:8]  # take first 8 to keep snapshot compact
    ]
    nodes = [
        {"free_mps": MPS_PER_GPU * gpus_per_node,
         "free_vram": MPS_PER_GPU * gpus_per_node, "running_jobs": 0}
        for _ in range(n_nodes)
    ]
    return {
        "ts": time.time(),
        "now": 0.0,
        "pending_jobs": pending,
        "nodes": nodes,
        "n_nodes": n_nodes,
        "gpus_per_node": gpus_per_node,
        "mps_per_gpu": MPS_PER_GPU,
    }


def build_slurmrestd_snapshot(slurmrestd_url: str) -> dict[str, Any]:
    """Live snapshot via slurmrestd. Uses the same parsing approach as
    operator/fragmentation.py (parses ``tres_used`` for mps slot counts)."""
    # Lazy import — only needed in live mode
    from operator_pkg_compat import nodes_from_slurm_rest  # type: ignore
    raise NotImplementedError(
        "Live slurmrestd snapshot path: wire from operator/fragmentation.py "
        "in Phase D when the live cluster is available."
    )


def push(serve_url: str, snap: dict[str, Any]) -> None:
    data = json.dumps(snap).encode("utf-8")
    req = urllib.request.Request(
        url=serve_url.rstrip("/") + "/snapshot",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as resp:
        body = resp.read().decode("utf-8")
        print(f"[snapshot_agent] push ok: {body}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--serve-url", default="http://127.0.0.1:8002")
    p.add_argument("--source", choices=["sim", "slurmrestd"], default="sim")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true")
    p.add_argument("--slurmrestd-url",
                   default=os.environ.get("SLURMRESTD_URL", ""))
    p.add_argument("--n-jobs", type=int, default=20)
    args = p.parse_args(argv)

    while True:
        try:
            if args.source == "sim":
                snap = build_sim_snapshot(n_jobs=args.n_jobs)
            else:
                snap = build_slurmrestd_snapshot(args.slurmrestd_url)
            push(args.serve_url, snap)
        except Exception as e:
            print(f"[snapshot_agent] error: {e}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
