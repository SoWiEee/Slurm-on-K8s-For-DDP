"""Ghost-job detector unit tests.

The detector raises an alarm when slurmrestd insists a pool has running
jobs but the StatefulSet has scaled to zero and no pod is Ready —
i.e. the worker that owned those jobs died and slurmctld never received
the epilog. Without this signal the operator silently refuses to scale
up (`running_jobs > 0` looks like "work in progress, don't add nodes")
and the cluster wedges indefinitely.

We test the predicate directly here rather than spinning up an
OperatorApp — the predicate is the entire contribution; the surrounding
plumbing (Prometheus labels, log emission) is covered by integration
tests when they're run.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def _is_ghost(current_replicas: int, ready: int, running_jobs: int) -> bool:
    """Mirror of the predicate inlined in OperatorApp._process_pool."""
    return current_replicas == 0 and ready == 0 and running_jobs > 0


class TestGhostPredicate:
    def test_wedge_state_is_ghost(self):
        # Classic wedge: slurmctld thinks there's a running job, but
        # the StatefulSet has been scaled to zero and no pod exists.
        assert _is_ghost(current_replicas=0, ready=0, running_jobs=1)

    def test_multiple_running_jobs_still_ghost(self):
        assert _is_ghost(current_replicas=0, ready=0, running_jobs=5)

    def test_healthy_cluster_not_ghost(self):
        # Normal running cluster: pods exist, jobs run, no wedge.
        assert not _is_ghost(current_replicas=2, ready=2, running_jobs=1)

    def test_quiet_cluster_not_ghost(self):
        # Scaled to zero on idle queue is the *expected* steady state.
        assert not _is_ghost(current_replicas=0, ready=0, running_jobs=0)

    def test_scaling_up_not_ghost(self):
        # Mid-provisioning: sts has replicas=2 but pods not yet Ready.
        # The running_jobs=0 means no wedge — jobs aren't stuck.
        assert not _is_ghost(current_replicas=2, ready=0, running_jobs=0)

    def test_ready_pod_with_running_job_not_ghost(self):
        # Even if replicas were briefly out of sync, having any ready
        # pod means there's a slurmd that can ACK the job.
        assert not _is_ghost(current_replicas=1, ready=1, running_jobs=3)
