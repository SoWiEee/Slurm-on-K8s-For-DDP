"""Gymnasium wrapper around the discrete-event runner for M11 RL training.

Inverts ``sim.runner``'s control flow: instead of a Scheduler.order() being
called by the runner after every event, the gym env steps event-by-event,
pauses at each scheduling-decision moment, exposes an observation, and
takes an action (which top-K queue job to schedule, or no-op).

MDP spec follows ``docs/scheduler.md`` §M11:
- State: top-K=16 pending jobs × 11 features + per-node state + global feat
- Action: Discrete(K+1), index K = no-op
- Reward: dense wait-penalty + sparse completion slowdown penalty
"""
from __future__ import annotations

import dataclasses
import heapq
import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
except ImportError as _e:  # pragma: no cover - guarded for env-less import
    gym = None  # type: ignore
    spaces = None  # type: ignore

from .cluster import Cluster
from .loader import Job, MPS_PER_GPU


TOP_K = 16
GPU_TYPES = ("rtx4070", "rtx4080", "a10", "h100")  # one-hot order
JOB_FEAT_DIM = 11  # see _job_feat()
NODE_FEAT_DIM = 3
GLOBAL_FEAT_DIM = 5
MAX_NODES = 4  # observation pads up to this; small-cluster setting


def _job_feat(job: Job, now: float, mps_per_gpu: int) -> np.ndarray:
    """11-dim per-job feature vector."""
    gpu_oh = [1.0 if job.gpu_type == t else 0.0 for t in GPU_TYPES]
    wait = max(0.0, now - job.submit_ts)
    return np.array([
        job.mps_req / mps_per_gpu,
        float(job.gpu_count),
        *gpu_oh,
        math.log1p(job.runtime),       # runtime "prediction" — sim uses true value
        math.log1p(wait),
        math.log1p(wait),              # age duplicate (no separate priority age yet)
        0.0,                           # deadline_remaining placeholder
        0.0,                           # retry_count placeholder
    ], dtype=np.float32)


def _node_feat(cluster: Cluster, ni: int) -> np.ndarray:
    node = cluster.nodes[ni]
    free_mps = node.free_mps_total()
    free_vram = sum(g.free_mps for g in node.gpus)  # proxy: same as MPS in sim
    running = sum(1 for plan in cluster.active.values() if any(a.node_id == ni for a in plan))
    return np.array([
        free_mps / (cluster.mps_per_gpu * cluster.gpus_per_node),
        free_vram / (cluster.mps_per_gpu * cluster.gpus_per_node),
        float(running),
    ], dtype=np.float32)


def _global_feat(pending: List[Job], cluster: Cluster, now: float) -> np.ndarray:
    queue_len = len(pending)
    if cluster.n_nodes > 1:
        free_per_node = cluster.free_mps_per_node()
        mean = max(1.0, sum(free_per_node) / len(free_per_node))
        var = sum((x - mean) ** 2 for x in free_per_node) / len(free_per_node)
        frag = math.sqrt(var) / mean
    else:
        frag = 0.0
    tod = (now % 86400) / 86400.0
    return np.array([
        math.log1p(queue_len),
        1.0,                          # predictor_spread placeholder (p90/p50)
        frag,
        math.sin(2 * math.pi * tod),
        math.cos(2 * math.pi * tod),
    ], dtype=np.float32)


@dataclass
class _RunState:
    """Mutable per-episode state held between step() calls."""
    cluster: Cluster
    pending: List[Job]
    events: list                 # heap of (time, seq, kind, payload)
    seq: int
    now: float
    by_id: dict
    completed: int
    n_jobs: int
    wait_accum_prev: float
    jct_sum: float
    completion_reward: float   # accumulated this-step completion bonus


class KubefluxSchedEnv:
    """Gymnasium-compatible discrete-event scheduling environment.

    Define as plain class so module imports without gymnasium installed
    (e.g. tooling); inherit/wrap at construction time.
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        jobs_factory: Callable[[], List[Job]],
        *,
        n_nodes: int = 2,
        gpus_per_node: int = 1,
        mps_per_gpu: int = MPS_PER_GPU,
        top_k: int = TOP_K,
        max_steps: int = 50_000,
    ) -> None:
        if gym is None:
            raise ImportError("gymnasium is not installed in this venv")
        self.jobs_factory = jobs_factory
        self.n_nodes = n_nodes
        self.gpus_per_node = gpus_per_node
        self.mps_per_gpu = mps_per_gpu
        self.top_k = top_k
        self.max_steps = max_steps
        self._step_count = 0
        self._state: Optional[_RunState] = None

        obs_dim = top_k * JOB_FEAT_DIM + MAX_NODES * NODE_FEAT_DIM + GLOBAL_FEAT_DIM
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_dim,), dtype=np.float32,
        )
        # K choices over top-K queue + 1 no-op
        self.action_space = spaces.Discrete(top_k + 1)

    # --------------------------------------------------------------
    def reset(self, *, seed=None, options=None):  # noqa: D401
        if seed is not None:
            np.random.seed(seed)
        jobs = self.jobs_factory()
        cluster = Cluster(
            n_nodes=self.n_nodes,
            gpus_per_node=self.gpus_per_node,
            mps_per_gpu=self.mps_per_gpu,
        )
        events: list = []
        seq = 0
        for j in sorted(jobs, key=lambda x: x.submit_ts):
            heapq.heappush(events, (j.submit_ts, seq, "submit", j.job_id))
            seq += 1
        self._state = _RunState(
            cluster=cluster,
            pending=[],
            events=events,
            seq=seq,
            now=0.0,
            by_id={j.job_id: j for j in jobs},
            completed=0,
            n_jobs=len(jobs),
            wait_accum_prev=0.0,
            jct_sum=0.0,
            completion_reward=0.0,
        )
        self._step_count = 0
        self._advance_to_decision()
        obs = self._build_obs()
        return obs, {}

    # --------------------------------------------------------------
    def step(self, action: int):
        if self._state is None:
            raise RuntimeError("call reset() first")
        st = self._state
        self._step_count += 1

        top = self._top_k_jobs()
        reward_action = 0.0
        scheduled = False
        if action < self.top_k and action < len(top):
            chosen = top[action]
            plan = st.cluster.try_allocate(chosen)
            if plan is not None:
                st.pending.remove(chosen)
                end_ts = st.now + chosen.runtime
                heapq.heappush(st.events, (end_ts, st.seq, "end", chosen.job_id))
                st.seq += 1
                scheduled = True
            else:
                # invalid pick (can't allocate) → small penalty
                reward_action = -0.01
        # If no allocation happened this step, force time forward by one event
        # to avoid infinite no-op loops while the queue stays stuck.
        st.completion_reward = 0.0
        if not scheduled and st.events:
            t, _s, kind, payload = heapq.heappop(st.events)
            st.now = t
            if kind == "submit":
                st.pending.append(st.by_id[payload])
            elif kind == "end":
                self._on_job_end(payload)

        self._advance_to_decision()

        # Dense reward: increase in summed pending wait time
        wait_accum = sum(max(0.0, st.now - j.submit_ts) for j in st.pending)
        dwait = wait_accum - st.wait_accum_prev
        st.wait_accum_prev = wait_accum
        reward = -dwait / 1000.0 + reward_action + st.completion_reward

        terminated = (st.completed >= st.n_jobs) and not st.events
        truncated = self._step_count >= self.max_steps
        obs = self._build_obs()
        info = {
            "now": st.now,
            "queue_len": len(st.pending),
            "completed": st.completed,
            "n_jobs": st.n_jobs,
        }
        info["jct_sum"] = st.jct_sum
        info["avg_jct"] = st.jct_sum / max(1, st.completed) if st.completed else float("nan")
        return obs, float(reward), bool(terminated), bool(truncated), info

    # --------------------------------------------------------------
    def _top_k_jobs(self) -> List[Job]:
        """Return up to top_k pending jobs, ordered by submit_ts (FCFS view)."""
        st = self._state
        assert st is not None
        return sorted(st.pending, key=lambda j: j.submit_ts)[: self.top_k]

    def _advance_to_decision(self) -> None:
        """Process events until a decision point: at least one pending job
        AND at least one feasible allocation, OR no more events.
        """
        st = self._state
        assert st is not None
        while st.events:
            # Decision point reached?
            if st.pending and any(st.cluster.can_allocate(j) for j in st.pending):
                return
            t, _s, kind, payload = heapq.heappop(st.events)
            st.now = t
            if kind == "submit":
                st.pending.append(st.by_id[payload])
            elif kind == "end":
                self._on_job_end(payload)
        # Drained: terminal state with possibly orphan pending (shouldn't happen)

    def _on_job_end(self, jid: str) -> None:
        """Centralised end-event handling: release cluster, accumulate JCT
        and completion-bonus reward."""
        st = self._state
        assert st is not None
        st.cluster.release(jid)
        st.completed += 1
        j = st.by_id[jid]
        jct = st.now - j.submit_ts
        st.jct_sum += jct
        # Slowdown bonus: r = -log(JCT / runtime). Perfect (JCT == runtime) → 0;
        # 2× slowdown → -0.69; 10× → -2.30. Clip to keep gradients stable.
        runtime = max(1.0, j.runtime)
        slowdown = max(1.0, jct / runtime)
        st.completion_reward += -math.log(slowdown)

    def _build_obs(self) -> np.ndarray:
        st = self._state
        assert st is not None
        top = self._top_k_jobs()
        feats = []
        for i in range(self.top_k):
            if i < len(top):
                feats.append(_job_feat(top[i], st.now, self.mps_per_gpu))
            else:
                feats.append(np.zeros(JOB_FEAT_DIM, dtype=np.float32))
        node_feats = []
        for ni in range(MAX_NODES):
            if ni < self.n_nodes:
                node_feats.append(_node_feat(st.cluster, ni))
            else:
                node_feats.append(np.zeros(NODE_FEAT_DIM, dtype=np.float32))
        glob = _global_feat(st.pending, st.cluster, st.now)
        return np.concatenate([*feats, *node_feats, glob]).astype(np.float32)

    def action_mask(self) -> np.ndarray:
        """Bool mask over action space — true = action is legal."""
        st = self._state
        assert st is not None
        top = self._top_k_jobs()
        mask = np.zeros(self.top_k + 1, dtype=bool)
        for i, j in enumerate(top):
            if st.cluster.can_allocate(j):
                mask[i] = True
        mask[self.top_k] = True  # no-op always legal
        return mask

    def render(self):  # noqa: D401
        return None

    def close(self):
        self._state = None


# Make subclass that registers with gymnasium properly
if gym is not None:
    class KubefluxSchedGymEnv(KubefluxSchedEnv, gym.Env):  # type: ignore
        pass
