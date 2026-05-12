"""Phase 6 M7 — fragmentation detector + requeue decider.

The operator's existing scaling loop only adds nodes; it never *moves*
jobs around. When the cluster is full of small low-priority jobs and a
larger high-priority job lands in the queue, scaling up doesn't help —
no new node is ever justified by a pending job that would fit on the
*existing* nodes if low-priority work was preempted. Gandiva-lite calls
this "fragmentation" and resolves it by killing the minimum set of
low-priority running jobs that would unblock the head of the queue;
the killed jobs come back as PENDING and are expected to resume from
checkpoint when they get rescheduled.

This module is intentionally pure-Python (no Slurm REST, no kubectl, no
prometheus-client). The operator wires it up in ``app.py`` by feeding
``JobView``/``NodeView`` lists and acting on the returned
``RequeueDecision``. Splitting that boundary keeps the detector
unit-testable from a fixture without needing a live cluster — see
``operator/tests/test_fragmentation.py``.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class JobView:
    """Minimal job snapshot. Built by ``app.py`` from squeue output."""
    job_id: str
    user: str
    partition: str
    state: str          # RUNNING / PENDING / SUSPENDED / ...
    priority: int       # multifactor priority; higher = more important
    mps_req: int        # MPS slots requested per node (1..mps_per_node)
    gpu_count: int      # whole GPUs requested
    nodes: Tuple[str, ...]  # node names this job is currently on (RUNNING only)
    submit_ts: float = 0.0
    runtime_seconds: float = 0.0  # how long it's been running, for tie-breaking


@dataclass(frozen=True)
class NodeView:
    """Per-node MPS-slot snapshot."""
    node_id: str
    free_mps: int
    total_mps: int

    @property
    def used_mps(self) -> int:
        return self.total_mps - self.free_mps


@dataclass(frozen=True)
class RequeueCandidate:
    """A running low-priority job that, if requeued, would free MPS slots
    on a node that a pending high-priority job needs."""
    victim: JobView
    target: JobView          # pending job this candidate would unblock
    node_id: str             # the node where freeing slots matters
    slots_freed: int


@dataclass(frozen=True)
class FragmentationSnapshot:
    timestamp: float
    score: float                                        # 0 (balanced) … 1 (worst)
    free_mps_per_node: Tuple[int, ...]
    pending_blocked: Tuple[JobView, ...]                # pending jobs that fit nowhere
    candidates: Tuple[RequeueCandidate, ...]            # potential preemption targets

    @property
    def has_actionable_fragmentation(self) -> bool:
        return bool(self.pending_blocked) and bool(self.candidates)


@dataclass(frozen=True)
class RequeueDecision:
    target_job_ids: Tuple[str, ...]      # which low-prio jobs to requeue
    blocked_job_ids: Tuple[str, ...]     # which pending jobs should now fit
    reason: str
    snapshot_score: float
    timestamp: float


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------
class FragmentationDetector:
    """Build a snapshot from current jobs + nodes.

    The detector does not actuate anything; it only shapes the candidate
    set the decider can choose from. Two configuration knobs:

      ``mps_per_node``     — Slurm gres.conf usually sets ``mps Count=100``;
                             override per pool when a deployment uses a
                             different mapping.
      ``priority_gap``     — minimum (target.priority - victim.priority)
                             gap in absolute multifactor units. Defaults to
                             0 (any positive gap counts), but operators
                             can require a more decisive lead before
                             paying the requeue cost.
    """
    def __init__(self, mps_per_node: int = 100, priority_gap: int = 0):
        self.mps_per_node = mps_per_node
        self.priority_gap = priority_gap

    @staticmethod
    def _fits_anywhere(job: JobView, nodes: Sequence[NodeView]) -> bool:
        return any(n.free_mps >= job.mps_req for n in nodes)

    @staticmethod
    def fragmentation_score(free: Sequence[int]) -> float:
        """Coefficient of variation, clamped to [0, 1].

        ``0`` = perfectly balanced free MPS across nodes (no fragmentation);
        ``1`` = maximum imbalance. Single-node clusters always return 0.
        """
        if len(free) < 2:
            return 0.0
        mean = statistics.fmean(free)
        if mean == 0:
            return 0.0
        cv = statistics.pstdev(free) / mean
        return max(0.0, min(1.0, cv))

    def snapshot(
        self,
        jobs: Iterable[JobView],
        nodes: Iterable[NodeView],
        *,
        now: float,
    ) -> FragmentationSnapshot:
        jobs = list(jobs)
        nodes = list(nodes)
        free = tuple(n.free_mps for n in nodes)

        pending = [j for j in jobs if j.state.upper() == "PENDING"]
        running = [j for j in jobs if j.state.upper() == "RUNNING"]

        # 1. pending jobs that fit nowhere (sorted by priority desc — the
        #    head of the blocked queue is what matters first)
        blocked = sorted(
            (j for j in pending if not self._fits_anywhere(j, nodes)),
            key=lambda j: (-j.priority, j.submit_ts, j.job_id),
        )

        # 2. for each blocked target, find running victims on a node where
        #    requeuing them would free enough slots. Lowest priority + oldest
        #    runtime first — matches the "least valuable progress lost" rule.
        candidates: List[RequeueCandidate] = []
        node_idx = {n.node_id: n for n in nodes}
        running_by_node: dict[str, list[JobView]] = {}
        for r in running:
            for nid in r.nodes:
                running_by_node.setdefault(nid, []).append(r)

        for target in blocked:
            for nid, occupants in running_by_node.items():
                node = node_idx.get(nid)
                if node is None:
                    continue
                deficit = target.mps_req - node.free_mps
                if deficit <= 0:
                    # Already fits on this node — but we said blocked.
                    # Skip; another blocked target may still need this node.
                    continue
                # Sort victims: lowest priority first, then oldest runtime.
                victims = sorted(
                    (
                        v for v in occupants
                        if (target.priority - v.priority) > self.priority_gap
                    ),
                    key=lambda v: (v.priority, -v.runtime_seconds, v.job_id),
                )
                # Emit ALL eligible victims (not just enough to cover the
                # deficit) so the decider can pick a subset by other rules
                # (lowest priority + oldest runtime).
                for victim in victims:
                    candidates.append(
                        RequeueCandidate(
                            victim=victim,
                            target=target,
                            node_id=nid,
                            slots_freed=victim.mps_req,
                        )
                    )

        return FragmentationSnapshot(
            timestamp=now,
            score=self.fragmentation_score(free),
            free_mps_per_node=free,
            pending_blocked=tuple(blocked),
            candidates=tuple(candidates),
        )


# ---------------------------------------------------------------------------
# Decider — turns a snapshot into an actuator-ready RequeueDecision.
# ---------------------------------------------------------------------------
class RequeueDecider:
    """Decide whether to act on a fragmentation snapshot, with rate limits.

    Two rate-limit knobs:

      ``min_interval_seconds``     — minimum spacing between any two
                                     requeue decisions. Stops a flapping
                                     cluster from issuing a requeue every
                                     reconcile.
      ``max_requeues_per_hour``    — sliding-window cap on decisions in
                                     any 3600-second window. The 6th
                                     attempt within an hour is rejected
                                     with reason="rate-limited".

    The decider keeps an in-memory ring of (timestamp, decision_count)
    tuples — there is no persistence across operator restarts (intentional;
    surviving a restart by waiting an hour is fine, surviving a restart
    *with* memory of the last 5 decisions would require state in a CRD
    which we don't have).
    """
    def __init__(
        self,
        *,
        min_interval_seconds: float = 60.0,
        max_requeues_per_hour: int = 5,
        max_targets_per_decision: int = 4,
    ) -> None:
        self.min_interval_seconds = min_interval_seconds
        self.max_requeues_per_hour = max_requeues_per_hour
        self.max_targets_per_decision = max_targets_per_decision
        self._history: List[float] = []   # timestamps of past decisions

    def _trim_history(self, now: float) -> None:
        cutoff = now - 3600.0
        self._history = [t for t in self._history if t >= cutoff]

    def _rate_limited(self, now: float) -> Optional[str]:
        self._trim_history(now)
        if self._history and (now - self._history[-1]) < self.min_interval_seconds:
            return f"min-interval ({self.min_interval_seconds:.0f}s)"
        if len(self._history) >= self.max_requeues_per_hour:
            return f"hourly-cap ({self.max_requeues_per_hour})"
        return None

    def decide(
        self,
        snapshot: FragmentationSnapshot,
        *,
        now: float,
    ) -> Tuple[Optional[RequeueDecision], str]:
        """Return (decision, reason). Decision is None when no action.

        ``reason`` is always populated; callers log it as the
        ``[fragmentation]`` line. When decision is non-None, ``reason``
        echoes the decision's own ``reason`` for log uniformity.
        """
        if not snapshot.has_actionable_fragmentation:
            return None, "no-fragmentation"

        limit_reason = self._rate_limited(now)
        if limit_reason is not None:
            return None, f"rate-limited:{limit_reason}"

        # Pick the highest-priority blocked target and the minimum set of
        # victims that would actually unblock it. We bias toward fewest
        # requeues — picking 1 large mps_req victim before 2 small ones.
        target = snapshot.pending_blocked[0]
        per_target = [c for c in snapshot.candidates if c.target.job_id == target.job_id]
        if not per_target:
            return None, "no-candidates-for-head"

        # Greedy by largest slots_freed so we kill fewer jobs.
        per_target_sorted = sorted(per_target, key=lambda c: (-c.slots_freed, c.victim.priority))

        # Pick the node with the most cumulative slots_freed across its
        # candidates — that's the node where killing fewest jobs will hit
        # the deficit. Then walk that node's candidates greedily.
        nodes_for_target = sorted(
            {c.node_id for c in per_target},
            key=lambda nid: -sum(c.slots_freed for c in per_target if c.node_id == nid),
        )
        chosen_node = nodes_for_target[0]
        on_node = [c for c in per_target_sorted if c.node_id == chosen_node]

        chosen: List[RequeueCandidate] = []
        seen_ids: set = set()
        slots_freed_so_far = 0
        # `target.mps_req` is the upper bound on what we ever need to free
        # (a fully-occupied node has deficit == target.mps_req). Conservative
        # over-kill on a partially-occupied node is fine — slurmctld places
        # the resumed jobs back when slots open up.
        for cand in on_node:
            if cand.victim.job_id in seen_ids:
                continue
            chosen.append(cand)
            seen_ids.add(cand.victim.job_id)
            slots_freed_so_far += cand.slots_freed
            if slots_freed_so_far >= target.mps_req:
                break
            if len(chosen) >= self.max_targets_per_decision:
                break

        if not chosen:
            return None, "no-victims-after-priority-gap"

        target_ids = tuple(c.victim.job_id for c in chosen)
        decision = RequeueDecision(
            target_job_ids=target_ids,
            blocked_job_ids=(target.job_id,),
            reason=(
                f"unblock {target.job_id} (priority {target.priority}, "
                f"mps_req {target.mps_req}) on {chosen_node}: "
                f"requeue {len(chosen)} job(s) freeing ~{slots_freed_so_far} slots"
            ),
            snapshot_score=snapshot.score,
            timestamp=now,
        )
        self._history.append(now)
        return decision, decision.reason

    # Read-only view of decision history — useful for tests and debug.
    def recent_decisions(self, *, now: float) -> List[float]:
        self._trim_history(now)
        return list(self._history)


# ---------------------------------------------------------------------------
# Slurm REST → View adapters
# ---------------------------------------------------------------------------
def _parse_mps_req(tres_per_node: str) -> int:
    """Mirrors the lua plugin's parser: ``mps:N`` or ``mps=N``."""
    if not tres_per_node:
        return 0
    import re
    m = re.search(r"mps[:=](\d+)", tres_per_node)
    return int(m.group(1)) if m else 0


def _parse_node_total_mps(gres: str) -> Optional[int]:
    """Parse a node's configured ``gres`` field for its total MPS slots.

    Slurm exposes the per-node configured GRES as ``gpu:rtx4070:1,mps:rtx4070:100``
    (typed) or ``mps:100`` (untyped). Returns ``None`` when the node has
    no MPS gres configured at all — caller treats those as 0/0 so the
    detector won't conclude that a CPU-only node "fits" an MPS-tagged
    pending job.
    """
    if not gres:
        return None
    import re
    m = re.search(r"(?:^|,)\s*mps(?::[A-Za-z0-9_-]+)?:(\d+)", gres)
    return int(m.group(1)) if m else None


def _parse_tres_mps(tres_used: str) -> Optional[int]:
    """Parse Slurm's ``tres_used`` field for ``gres/mps=N``.

    On a live cluster the canonical MPS-slot allocation lives in
    ``tres_used`` (e.g. ``cpu=4,gres/mps=50``) — ``gres_used`` carries
    a different shape (``mps:<TYPE>:<count>(IDX:<indices>)``) where the
    bare number after the first colon is the *device count*, not the
    slot count. Returns ``None`` when no ``gres/mps=`` token appears so
    the caller can fall back to ``gres_used``.
    """
    if not tres_used:
        return None
    import re
    m = re.search(r"gres/mps=(\d+)", tres_used)
    return int(m.group(1)) if m else None


def _parse_node_list(node_list: str) -> Tuple[str, ...]:
    """``slurm-worker-cpu-[0-2]`` → (slurm-worker-cpu-0, slurm-worker-cpu-1, slurm-worker-cpu-2).

    Slurm node lists support ranges and comma-separated lists. We support
    the common forms; anything weird falls through as a single entry.
    """
    if not node_list or node_list in ("(null)", "N/A", ""):
        return ()
    if "[" not in node_list:
        # bare list: "n1,n2"
        return tuple(p.strip() for p in node_list.split(",") if p.strip())
    # bracketed: "prefix-[0-2,4]"
    import re
    m = re.match(r"([^[]+)\[([^\]]+)\]$", node_list)
    if not m:
        return (node_list,)
    prefix, ranges = m.group(1), m.group(2)
    out: List[str] = []
    for part in ranges.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            try:
                for i in range(int(lo), int(hi) + 1):
                    out.append(f"{prefix}{i}")
            except ValueError:
                out.append(f"{prefix}{part}")
        else:
            out.append(f"{prefix}{part}")
    return tuple(out)


def jobs_from_slurm_rest(rest_jobs: Iterable[dict], *, mps_per_node: int = 100) -> List[JobView]:
    """Map ``SlurmRestClient.list_jobs(...)`` output into ``JobView``."""
    out: List[JobView] = []
    for j in rest_jobs:
        state = str(j.get("job_state", "") or "").upper()
        if state not in ("RUNNING", "PENDING"):
            continue
        tres = j.get("tres_per_node") or ""
        if isinstance(tres, list):
            tres = ",".join(tres)
        mps = _parse_mps_req(str(tres))
        if mps == 0:
            mps = mps_per_node  # whole-node when no MPS request
        gpu_count = 1
        # Slurm exposes either gres_detail (per-node) or num_nodes.
        try:
            gpu_count = int(j.get("num_nodes", 1) or 1)
        except (TypeError, ValueError):
            pass
        nodes = _parse_node_list(str(j.get("nodes") or ""))
        out.append(JobView(
            job_id=str(j.get("job_id", "")),
            user=str(j.get("user_name", j.get("user", "anon"))),
            partition=str(j.get("partition", "")),
            state=state,
            priority=int(j.get("priority", 0) or 0),
            mps_req=mps,
            gpu_count=gpu_count,
            nodes=nodes,
            submit_ts=float(j.get("submit_time", 0) or 0),
            runtime_seconds=float(j.get("run_time", 0) or 0),
        ))
    return out


_UNAVAILABLE_NODE_STATES = frozenset({
    "drain", "drained", "draining", "down", "fail", "failing",
    "maint", "reserved", "powering_down", "powered_down", "future",
    "not_responding", "no_respond",
})


def _node_is_available(node: dict) -> bool:
    raw = node.get("state", "")
    states: List[str]
    if isinstance(raw, list):
        states = [str(s).lower() for s in raw]
    else:
        states = [s.strip().lower() for s in str(raw).replace("+", ",").split(",") if s.strip()]
    return not any(s in _UNAVAILABLE_NODE_STATES for s in states)


def nodes_from_slurm_rest(rest_nodes: Iterable[dict], *, mps_per_node: int = 100) -> List[NodeView]:
    """Map ``SlurmRestClient.list_nodes()`` output into ``NodeView``.

    Slurm REST exposes per-GRES allocations under ``gres_used`` as a
    string like ``gpu:rtx4070:1,mps:75`` (75 of 100 MPS slots used). We
    parse that to derive ``free_mps`` per node; nodes without ``mps``
    GRES report 0 used (treated as full ``mps_per_node`` free).

    Nodes in DRAIN / DOWN / MAINT / RESERVED states are reported with
    ``free_mps=0`` so the detector won't conclude that a blocked
    pending job "fits there" — Slurm's scheduler won't place jobs on
    them either.
    """
    out: List[NodeView] = []
    for n in rest_nodes:
        # Per-node total MPS comes from the node's configured ``gres``;
        # nodes without an mps gres (e.g. CPU-only pools) get 0/0 so they
        # never look like a place an MPS-tagged pending job could fit.
        has_gres_key = "gres" in n
        gres_cfg = n.get("gres") or ""
        if isinstance(gres_cfg, list):
            gres_cfg = ",".join(gres_cfg)
        total = _parse_node_total_mps(str(gres_cfg))
        if total is None:
            # Node provided gres metadata but it doesn't mention MPS →
            # this node has no MPS capacity. Only fall back to the
            # cluster-wide default when a fixture omits the field
            # entirely (older test data has no "gres" key at all).
            total = 0 if has_gres_key else mps_per_node

        # Prefer tres_used's "gres/mps=N" — that's the actual slot-count
        # allocation. Fall back to gres_used for back-compat with fixtures
        # that supply a bare "mps:N" string.
        tres_used = n.get("tres_used") or ""
        if isinstance(tres_used, list):
            tres_used = ",".join(tres_used)
        used_from_tres = _parse_tres_mps(str(tres_used))
        if used_from_tres is not None:
            used = used_from_tres
        else:
            gres_used = n.get("gres_used") or ""
            if isinstance(gres_used, list):
                gres_used = ",".join(gres_used)
            used = _parse_mps_req(str(gres_used))
        free = max(0, total - used)
        if not _node_is_available(n):
            free = 0
        out.append(NodeView(
            node_id=str(n.get("name", "")),
            free_mps=free,
            total_mps=total,
        ))
    return out


# ---------------------------------------------------------------------------
# Reconciler — orchestrates detect → decide → actuate.
# ---------------------------------------------------------------------------
@dataclass
class ReconcileResult:
    snapshot: FragmentationSnapshot
    decision: Optional[RequeueDecision]
    reason: str
    requeued: Tuple[str, ...] = ()
    shadow: bool = False
    actuator_errors: Tuple[str, ...] = ()


class FragmentationReconciler:
    """Runs detect → decide → (optional) actuate as a single tick.

    The actuator is a callable ``(job_id) -> None`` that issues the
    Slurm requeue RPC; in production this is a thin wrapper around
    ``scontrol requeue <job_id>`` (or the slurmrestd POST equivalent).
    Keeping the dependency injected makes the reconciler trivially
    testable from a fixture — no kubectl, no HTTP.

    ``shadow_mode=True`` runs the full pipeline but never invokes the
    actuator. Lets us evaluate the decider in production for a release
    cycle before turning real preemption on.
    """
    def __init__(
        self,
        detector: FragmentationDetector,
        decider: RequeueDecider,
        actuator,
        *,
        shadow_mode: bool = False,
    ) -> None:
        self.detector = detector
        self.decider = decider
        self.actuator = actuator
        self.shadow_mode = shadow_mode

    def reconcile(
        self,
        jobs: Iterable[JobView],
        nodes: Iterable[NodeView],
        *,
        now: float,
    ) -> ReconcileResult:
        snapshot = self.detector.snapshot(jobs, nodes, now=now)
        decision, reason = self.decider.decide(snapshot, now=now)
        if decision is None or self.shadow_mode:
            return ReconcileResult(
                snapshot=snapshot,
                decision=decision,
                reason=reason,
                shadow=self.shadow_mode,
            )
        errors: List[str] = []
        succeeded: List[str] = []
        for jid in decision.target_job_ids:
            try:
                self.actuator(jid)
                succeeded.append(jid)
            except Exception as e:
                errors.append(f"{jid}:{type(e).__name__}:{e}")
        return ReconcileResult(
            snapshot=snapshot,
            decision=decision,
            reason=reason,
            requeued=tuple(succeeded),
            actuator_errors=tuple(errors),
        )
