#!/usr/bin/env bash
# verify-fragmentation.sh — Phase 6 M7 acceptance.
#
# Five gates, all offline (no live cluster needed):
#
#   1. pytest under operator/tests/test_fragmentation.py     (16 cases)
#   2. helm-unittest operator_test.yaml + workers_test.yaml  (M7 cases)
#   3. acceptance scenario harness — 4×mps:25 fully occupy a node, a
#      mps:50 lands PENDING, FragmentationReconciler issues a requeue
#      decision targeting 2 victims (50-slot deficit / 25-slot victims)
#   4. rate-limit harness — force 6 decisions in a row, the 6th is
#      rejected with "rate-limited:hourly-cap"
#   5. shadow-mode harness — same scenario as #3 but shadow_mode=True
#      runs the full pipeline, NO actuator call
#
# Bullet "live sbatch + scontrol requeue" lives in the M8 evaluation
# story; M7 is the plumbing milestone.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY=${PY:-.venv-m5/bin/python}

step() { printf "\n[verify-fr] %s\n" "$*"; }
fail() { printf "\n[verify-fr] FAIL: %s\n" "$*" >&2; exit 1; }

[[ -x "$PY" ]] || fail "python venv not at $PY (run: uv venv .venv-m5 && uv pip install --python .venv-m5/bin/python pytest prometheus-client kubernetes urllib3)"

# ---------------------------------------------------------------------------
step "1/5 pytest fragmentation unit tests"
"$PY" -m pytest -q operator/tests/test_fragmentation.py \
  || fail "pytest failed"

# ---------------------------------------------------------------------------
step "2/5 helm-unittest operator + workers (M7 cases)"
helm unittest "$ROOT/chart" -f 'tests/operator_test.yaml' -f 'tests/workers_test.yaml' \
  || fail "helm-unittest failed"

# ---------------------------------------------------------------------------
step "3/5 acceptance scenario — 4×mps:25 + 1 mps:50 → 2-victim requeue"
"$PY" - <<'PY' || fail "acceptance scenario failed"
import sys
sys.path.insert(0, "operator")
from fragmentation import (
    FragmentationDetector, RequeueDecider, FragmentationReconciler,
    JobView, NodeView,
)

def J(jid, *, state, prio, mps, nodes=()):
    return JobView(job_id=jid, user="u", partition="gpu", state=state,
                   priority=prio, mps_req=mps, gpu_count=1, nodes=tuple(nodes),
                   submit_ts=0.0, runtime_seconds=0.0)

invoked = []
det = FragmentationDetector(mps_per_node=100)
dec = RequeueDecider(min_interval_seconds=0, max_requeues_per_hour=10)
rec = FragmentationReconciler(det, dec, invoked.append, shadow_mode=False)

jobs = [
    J("r1", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r2", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r3", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r4", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("p1", state="PENDING", prio=900, mps=50),
]
nodes = [NodeView("n1", free_mps=0, total_mps=100)]
res = rec.reconcile(jobs, nodes, now=1000.0)

print("  reason           :", res.reason)
print("  snapshot.score   :", round(res.snapshot.score, 4))
print("  blocked          :", [j.job_id for j in res.snapshot.pending_blocked])
print("  candidates       :", [(c.victim.job_id, c.slots_freed) for c in res.snapshot.candidates])
print("  decision.targets :", res.decision.target_job_ids if res.decision else None)
print("  requeued         :", res.requeued)
assert res.decision is not None, "expected a decision"
assert len(res.decision.target_job_ids) == 2, "expected 2 victims for 50-slot deficit"
assert set(res.decision.target_job_ids) <= {"r1","r2","r3","r4"}
assert tuple(invoked) == res.requeued
print("  → ok")
PY

# ---------------------------------------------------------------------------
step "4/5 rate-limit harness — 6th decision rejected with hourly-cap"
"$PY" - <<'PY' || fail "rate-limit harness failed"
import sys
sys.path.insert(0, "operator")
from fragmentation import (
    FragmentationDetector, RequeueDecider, FragmentationReconciler,
    JobView, NodeView,
)

det = FragmentationDetector(mps_per_node=100)
dec = RequeueDecider(min_interval_seconds=0, max_requeues_per_hour=5)

def J(jid, *, state, prio, mps, nodes=()):
    return JobView(job_id=jid, user="u", partition="gpu", state=state,
                   priority=prio, mps_req=mps, gpu_count=1, nodes=tuple(nodes),
                   submit_ts=0.0, runtime_seconds=0.0)

scenario = [
    J("r1", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r2", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r3", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r4", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("p1", state="PENDING", prio=900, mps=50),
]
nodes = [NodeView("n1", free_mps=0, total_mps=100)]

results = []
for i in range(6):
    snap = det.snapshot(scenario, nodes, now=1000.0 + i)
    d, reason = dec.decide(snap, now=1000.0 + i)
    results.append((d is not None, reason))
    print(f"  attempt {i+1}: ok={d is not None} reason={reason}")

ok = [r[0] for r in results]
reasons = [r[1] for r in results]
assert ok[:5] == [True]*5, f"first 5 should pass: {ok}"
assert ok[5] is False,    "6th should be rate-limited"
assert "hourly-cap" in reasons[5], f"expected hourly-cap, got {reasons[5]}"
print("  → ok (5 passes + 1 hourly-cap reject)")
PY

# ---------------------------------------------------------------------------
step "5/5 shadow-mode harness — actuator never invoked"
"$PY" - <<'PY' || fail "shadow-mode harness failed"
import sys
sys.path.insert(0, "operator")
from fragmentation import (
    FragmentationDetector, RequeueDecider, FragmentationReconciler,
    JobView, NodeView,
)

invoked = []
det = FragmentationDetector(mps_per_node=100)
dec = RequeueDecider(min_interval_seconds=0)
rec = FragmentationReconciler(det, dec, invoked.append, shadow_mode=True)

def J(jid, *, state, prio, mps, nodes=()):
    return JobView(job_id=jid, user="u", partition="gpu", state=state,
                   priority=prio, mps_req=mps, gpu_count=1, nodes=tuple(nodes),
                   submit_ts=0.0, runtime_seconds=0.0)

jobs = [
    J("r1", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r2", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r3", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("r4", state="RUNNING", prio=100, mps=25, nodes=["n1"]),
    J("p1", state="PENDING", prio=900, mps=50),
]
res = rec.reconcile(jobs, [NodeView("n1", free_mps=0, total_mps=100)], now=1000.0)
assert res.decision is not None, "shadow mode must still emit a decision"
assert res.shadow is True
assert invoked == [], "actuator must NOT be called in shadow mode"
assert res.requeued == ()
print("  → ok (decision emitted, actuator untouched)")
PY

step "M7 verify ok"
