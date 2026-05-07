"""Phase 6 M7 — fragmentation detector + decider unit tests."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from fragmentation import (  # noqa: E402
    FragmentationDetector,
    FragmentationReconciler,
    JobView,
    NodeView,
    RequeueDecider,
    jobs_from_slurm_rest,
    nodes_from_slurm_rest,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _job(jid, *, state, prio, mps, nodes=(), gpu=1, runtime=0.0):
    return JobView(
        job_id=str(jid), user="u", partition="gpu", state=state,
        priority=prio, mps_req=mps, gpu_count=gpu, nodes=tuple(nodes),
        submit_ts=0.0, runtime_seconds=runtime,
    )


def _node(nid, *, free, total=100):
    return NodeView(node_id=nid, free_mps=free, total_mps=total)


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------
class TestDetector:
    def test_no_blocked_jobs_when_pending_fits(self):
        det = FragmentationDetector(mps_per_node=100)
        jobs = [_job("p1", state="PENDING", prio=500, mps=25)]
        nodes = [_node("n1", free=50)]
        snap = det.snapshot(jobs, nodes, now=0)
        assert snap.pending_blocked == ()
        assert snap.candidates == ()

    def test_blocked_pending_with_no_running_returns_no_candidates(self):
        det = FragmentationDetector()
        jobs = [_job("p1", state="PENDING", prio=500, mps=50)]
        nodes = [_node("n1", free=10)]
        snap = det.snapshot(jobs, nodes, now=0)
        assert len(snap.pending_blocked) == 1
        assert snap.candidates == ()
        assert not snap.has_actionable_fragmentation

    def test_acceptance_scenario_4x25_blocks_one_50(self):
        """The M7 acceptance scenario from scheduler.md."""
        det = FragmentationDetector(mps_per_node=100)
        jobs = [
            _job("r1", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r2", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r3", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r4", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("p1", state="PENDING", prio=900, mps=50),
        ]
        nodes = [_node("n1", free=0)]  # fully occupied
        snap = det.snapshot(jobs, nodes, now=0)
        assert [j.job_id for j in snap.pending_blocked] == ["p1"]
        # All 4 r* are eligible victims (priority 100 < 900).
        assert {c.victim.job_id for c in snap.candidates} == {"r1", "r2", "r3", "r4"}
        assert snap.has_actionable_fragmentation

    def test_priority_gap_filters_victims(self):
        det = FragmentationDetector(mps_per_node=100, priority_gap=500)
        jobs = [
            _job("r1", state="RUNNING", prio=600, mps=25, nodes=("n1",)),  # gap=300, filtered
            _job("r2", state="RUNNING", prio=200, mps=25, nodes=("n1",)),  # gap=700, eligible
            _job("p1", state="PENDING", prio=900, mps=50),
        ]
        nodes = [_node("n1", free=50)]   # not blocked actually
        # Force blocked: free=0 → blocked
        nodes = [_node("n1", free=0)]
        snap = det.snapshot(jobs, nodes, now=0)
        assert {c.victim.job_id for c in snap.candidates} == {"r2"}

    def test_fragmentation_score_balanced_vs_skewed(self):
        det = FragmentationDetector()
        assert det.fragmentation_score([50, 50, 50]) == pytest.approx(0.0)
        assert det.fragmentation_score([0, 100]) > 0.5
        # single-node always 0
        assert det.fragmentation_score([42]) == 0.0


# ---------------------------------------------------------------------------
# Decider
# ---------------------------------------------------------------------------
class TestDecider:
    def _scenario_snapshot(self, det):
        jobs = [
            _job("r1", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r2", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r3", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r4", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("p1", state="PENDING", prio=900, mps=50),
        ]
        return det.snapshot(jobs, [_node("n1", free=0)], now=1000.0)

    def test_decision_unblocks_head_with_minimum_kills(self):
        det = FragmentationDetector(mps_per_node=100)
        dec = RequeueDecider(min_interval_seconds=0)
        snap = self._scenario_snapshot(det)
        decision, reason = dec.decide(snap, now=1000.0)
        assert decision is not None, reason
        # 50-slot deficit + 25-slot victims → 2 kills
        assert len(decision.target_job_ids) == 2
        assert decision.blocked_job_ids == ("p1",)

    def test_no_fragmentation_returns_none(self):
        det = FragmentationDetector()
        dec = RequeueDecider()
        snap = det.snapshot([], [_node("n1", free=100)], now=0)
        d, reason = dec.decide(snap, now=0)
        assert d is None
        assert reason == "no-fragmentation"

    def test_min_interval_blocks_back_to_back_decisions(self):
        det = FragmentationDetector()
        dec = RequeueDecider(min_interval_seconds=60)
        snap = self._scenario_snapshot(det)
        first, _ = dec.decide(snap, now=1000.0)
        assert first is not None
        second, reason = dec.decide(snap, now=1010.0)  # 10s later
        assert second is None
        assert "min-interval" in reason

    def test_hourly_cap_rejects_after_max_decisions(self):
        det = FragmentationDetector()
        dec = RequeueDecider(min_interval_seconds=0, max_requeues_per_hour=5)
        snap = self._scenario_snapshot(det)
        for i in range(5):
            d, _ = dec.decide(snap, now=1000.0 + i)
            assert d is not None
        # 6th attempt — within the hour, must be capped
        d6, reason = dec.decide(snap, now=1010.0)
        assert d6 is None
        assert "hourly-cap" in reason
        # Outside the hour window — allowed again
        d7, _ = dec.decide(snap, now=1000.0 + 3601.0)
        assert d7 is not None

    def test_max_targets_per_decision_clamp(self):
        det = FragmentationDetector(mps_per_node=100)
        dec = RequeueDecider(min_interval_seconds=0, max_targets_per_decision=1)
        snap = self._scenario_snapshot(det)
        d, _ = dec.decide(snap, now=1000.0)
        assert d is not None
        assert len(d.target_job_ids) == 1


# ---------------------------------------------------------------------------
# Slurm REST adapter
# ---------------------------------------------------------------------------
class TestSlurmRestAdapter:
    def test_jobs_from_slurm_rest_parses_running_pending(self):
        rest = [
            {
                "job_id": 101, "user_name": "alice", "partition": "gpu",
                "job_state": "RUNNING", "priority": 100,
                "tres_per_node": "gpu:rtx4070:1,mps:25",
                "num_nodes": 1, "nodes": "slurm-worker-gpu-0",
            },
            {
                "job_id": 102, "user_name": "bob", "partition": "gpu",
                "job_state": "PENDING", "priority": 900,
                "tres_per_node": "mps:50", "num_nodes": 1, "nodes": "(null)",
            },
            {
                "job_id": 103, "user_name": "x", "partition": "gpu",
                "job_state": "COMPLETING", "priority": 0,
            },
        ]
        out = jobs_from_slurm_rest(rest, mps_per_node=100)
        assert len(out) == 2
        assert out[0].mps_req == 25
        assert out[0].nodes == ("slurm-worker-gpu-0",)
        assert out[1].state == "PENDING"
        assert out[1].mps_req == 50

    def test_nodes_from_slurm_rest_derives_free_mps(self):
        rest = [
            {"name": "n0", "gres_used": "gpu:rtx4070:1,mps:75"},
            {"name": "n1", "gres_used": "gpu:rtx4070:0,mps:0"},
            {"name": "n2", "gres_used": ""},
        ]
        nodes = nodes_from_slurm_rest(rest, mps_per_node=100)
        assert nodes[0].free_mps == 25
        assert nodes[1].free_mps == 100
        assert nodes[2].free_mps == 100

    def test_node_list_range_expansion(self):
        from fragmentation import _parse_node_list
        assert _parse_node_list("slurm-worker-gpu-[0-2]") == (
            "slurm-worker-gpu-0",
            "slurm-worker-gpu-1",
            "slurm-worker-gpu-2",
        )
        assert _parse_node_list("n1,n2") == ("n1", "n2")
        assert _parse_node_list("(null)") == ()


# ---------------------------------------------------------------------------
# Reconciler — drives detect→decide→actuate end-to-end with a mock actuator
# ---------------------------------------------------------------------------
class TestReconciler:
    def test_actuator_fires_for_each_target(self):
        det = FragmentationDetector(mps_per_node=100)
        dec = RequeueDecider(min_interval_seconds=0)
        invoked: list[str] = []
        rec = FragmentationReconciler(det, dec, invoked.append, shadow_mode=False)

        jobs = [
            _job("r1", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r2", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r3", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r4", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("p1", state="PENDING", prio=900, mps=50),
        ]
        result = rec.reconcile(jobs, [_node("n1", free=0)], now=1000.0)
        assert result.decision is not None
        assert len(invoked) == len(result.decision.target_job_ids)
        assert set(invoked).issubset({"r1", "r2", "r3", "r4"})
        assert tuple(invoked) == result.requeued

    def test_shadow_mode_never_invokes_actuator(self):
        det = FragmentationDetector(mps_per_node=100)
        dec = RequeueDecider(min_interval_seconds=0)
        invoked: list[str] = []
        rec = FragmentationReconciler(det, dec, invoked.append, shadow_mode=True)
        jobs = [
            _job("r1", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r2", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r3", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r4", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("p1", state="PENDING", prio=900, mps=50),
        ]
        result = rec.reconcile(jobs, [_node("n1", free=0)], now=1000.0)
        assert result.decision is not None
        assert result.shadow is True
        assert invoked == []
        assert result.requeued == ()

    def test_actuator_errors_collected_not_raised(self):
        det = FragmentationDetector(mps_per_node=100)
        dec = RequeueDecider(min_interval_seconds=0)
        def boom(jid):
            raise RuntimeError(f"scontrol blew up on {jid}")
        rec = FragmentationReconciler(det, dec, boom, shadow_mode=False)
        jobs = [
            _job("r1", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r2", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r3", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("r4", state="RUNNING", prio=100, mps=25, nodes=("n1",)),
            _job("p1", state="PENDING", prio=900, mps=50),
        ]
        result = rec.reconcile(jobs, [_node("n1", free=0)], now=1000.0)
        assert result.requeued == ()
        assert len(result.actuator_errors) == len(result.decision.target_job_ids)
        assert all("RuntimeError" in e for e in result.actuator_errors)
