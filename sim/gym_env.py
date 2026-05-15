"""Gymnasium wrapper around the discrete-event runner for DRL training.

MDP spec (placement-aware):
- State : top-K=16 pending jobs × 11 feats
          + 2 nodes × 2 GPUs × 6 feats   (GPU slot state)
          + 4 topology feats
          + 6 global feats
          = 210 dims
- Action: Discrete(N_JOBS × N_NODES × N_GPUS + 1)
          = 16 × 2 × 2 + 1 = 65
          action a = job_i * (N_NODES*N_GPUS) + node_j * N_GPUS + gpu_k
          action 64 = no-op
- Reward: r_placement (per-action) + r_completion (per-job-end)
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
except ImportError:
    gym = None  # type: ignore
    spaces = None  # type: ignore

from .cluster import Cluster
from .loader import Job, MPS_PER_GPU

# ── Layout constants ───────────────────────────────────────────────────────
TOP_K     = 16
GPU_TYPES = ("rtx4070", "rtx4080", "a10", "h100")

JOB_FEAT_DIM    = 11
GPU_FEAT_DIM    = 6
TOPO_FEAT_DIM   = 4
GLOBAL_FEAT_DIM = 6

# ── Cluster size — current deployment vs. target ───────────────────────────
# LIVE (current): 1 host × 1 GPU (RTX 4070, MPS enabled)
#   obs_dim  = 16*11 + 1*1*6 + 4 + 6 = 192
#   n_actions = 16*1*1 + 1 = 17
#
# SIM training default (2×2): mirrors target 2-host cluster
#   obs_dim  = 16*11 + 2*2*6 + 4 + 6 = 210
#   n_actions = 16*2*2 + 1 = 65
#
# HOW TO ADD A SECOND GPU:
#   1. Set N_NODES=2, N_GPUS=2 below (or pass n_nodes=2, gpus_per_node=2 to env)
#   2. Retrain DSAC from scratch (obs_dim 192→210, n_actions 17→65 — checkpoint
#      is NOT compatible; different network input/output shape)
#   3. Update rlpd_finetune.py / hierarchical.py CLI defaults to match
#   4. In Slurm: verify two worker nodes are registered and GRES is correct
N_NODES = 1   # current: single host  ← change to 2 when second GPU is online
N_GPUS  = 1   # current: single GPU   ← change to 2 when second GPU is online

# Derived defaults (reflect N_NODES / N_GPUS above)
N_PLACEMENTS = N_NODES * N_GPUS
N_ACTIONS    = TOP_K * N_PLACEMENTS + 1
NO_OP        = N_ACTIONS - 1
OBS_DIM      = (TOP_K * JOB_FEAT_DIM
                + N_NODES * N_GPUS * GPU_FEAT_DIM
                + TOPO_FEAT_DIM
                + GLOBAL_FEAT_DIM)


def env_dims(n_nodes: int, gpus_per_node: int, top_k: int = TOP_K) -> tuple[int, int]:
    """Return (obs_dim, n_actions) for a given cluster shape.

    Use this to construct a matching DSACAgent before creating the env::

        obs_dim, n_actions = env_dims(n_nodes=1, gpus_per_node=1)
        agent = DSACAgent(obs_dim=obs_dim, n_actions=n_actions)
        env = KubefluxSchedEnv(..., n_nodes=1, gpus_per_node=1)
    """
    obs = (top_k * JOB_FEAT_DIM
           + n_nodes * gpus_per_node * GPU_FEAT_DIM
           + TOPO_FEAT_DIM
           + GLOBAL_FEAT_DIM)
    n_act = top_k * n_nodes * gpus_per_node + 1
    return obs, n_act

# Bandwidth placeholders (sim has no real network model; feats are 1.0 = full)
_INTRA_BW_TOTAL = 1.0
_INTER_BW_TOTAL = 1.0


# ── Feature extractors ────────────────────────────────────────────────────

def _job_feat(job: Job, now: float, mps_per_gpu: int) -> np.ndarray:
    """11-dim per-job feature vector."""
    gpu_oh = [1.0 if job.gpu_type == t else 0.0 for t in GPU_TYPES]
    wait = max(0.0, now - job.submit_ts)
    return np.array([
        job.mps_req / mps_per_gpu,
        float(job.gpu_count),
        *gpu_oh,                        # 4 dims
        math.log1p(job.runtime),
        math.log1p(wait),
        math.log1p(wait),               # age (duplicate until priority age added)
        0.0,                            # deadline_remaining placeholder
        0.0,                            # retry_count placeholder
    ], dtype=np.float32)


def _gpu_feat(cluster: Cluster, node_i: int, gpu_i: int) -> np.ndarray:
    """6-dim per-GPU feature vector."""
    node = cluster.nodes[node_i]
    gpu  = node.gpus[gpu_i]
    total_mps = cluster.mps_per_gpu
    free_ratio  = gpu.free_mps / total_mps
    vram_ratio  = gpu.free_mps / total_mps   # proxy: same scale as MPS in sim
    running_on_gpu = sum(
        1 for plan in cluster.active.values()
        for alloc in plan
        if alloc.node_id == node_i and gpu_i in alloc.gpu_indices
    )
    # gpu_type one-hot — currently homogeneous cluster (all rtx4070)
    gpu_type_oh = [1.0, 0.0, 0.0]   # rtx4070 / other_a / other_b
    return np.array([
        free_ratio,
        vram_ratio,
        float(running_on_gpu),
        *gpu_type_oh,
    ], dtype=np.float32)


def _topo_feat(pending: List[Job], cluster: Cluster) -> np.ndarray:
    """4-dim topology feature vector."""
    # In sim, bandwidth is not modelled — placeholders reflect "full capacity"
    intra_bw = 1.0
    inter_bw  = 1.0
    # fraction of pending jobs needing >1 GPU (DDP pressure)
    ddp_ratio = (sum(1 for j in pending if j.gpu_count > 1) / max(1, len(pending)))
    # number of currently running cross-node allocations
    cross_node = sum(
        1 for plan in cluster.active.values()
        if len({alloc.node_id for alloc in plan}) > 1
    )
    return np.array([intra_bw, inter_bw, ddp_ratio, float(cross_node)], dtype=np.float32)


def _global_feat(pending: List[Job], cluster: Cluster, now: float) -> np.ndarray:
    """6-dim global feature vector."""
    queue_len = len(pending)
    # predictor_spread: p90/p50 of pending runtimes (oracle in sim)
    if len(pending) >= 2:
        rts = sorted(j.runtime for j in pending)
        n   = len(rts)
        p50 = rts[int(n * 0.50)]
        p90 = rts[min(int(n * 0.90), n - 1)]
        spread = (p90 / p50) if p50 > 0 else 1.0
    else:
        spread = 1.0
    # fragmentation: coefficient of variation of free MPS across nodes
    if cluster.n_nodes > 1:
        free_per_node = cluster.free_mps_per_node()
        mean = max(1.0, sum(free_per_node) / len(free_per_node))
        var  = sum((x - mean) ** 2 for x in free_per_node) / len(free_per_node)
        frag = math.sqrt(var) / mean
    else:
        frag = 0.0
    tod = (now % 86400) / 86400.0
    return np.array([
        math.log1p(queue_len),
        spread,
        frag,
        math.sin(2 * math.pi * tod),
        math.cos(2 * math.pi * tod),
        0.0,   # reserved
    ], dtype=np.float32)


# ── Action codec ──────────────────────────────────────────────────────────

def encode_action(job_i: int, node_j: int, gpu_k: int) -> int:
    """Encode (job, node, gpu) triple into a flat action index."""
    return job_i * N_PLACEMENTS + node_j * N_GPUS + gpu_k


def decode_action(a: int) -> Tuple[int, int, int]:
    """Decode flat action index into (job_i, node_j, gpu_k). Raises if no-op."""
    if a == NO_OP:
        raise ValueError("no-op has no (job, node, gpu) decomposition")
    job_i  = a // N_PLACEMENTS
    rem    = a %  N_PLACEMENTS
    node_j = rem // N_GPUS
    gpu_k  = rem %  N_GPUS
    return job_i, node_j, gpu_k


# ── Run state ─────────────────────────────────────────────────────────────

@dataclass
class _RunState:
    cluster:          Cluster
    pending:          List[Job]
    events:           list
    seq:              int
    now:              float
    by_id:            dict
    completed:        int
    n_jobs:           int
    jct_sum:          float
    completion_reward: float


# ── Environment ───────────────────────────────────────────────────────────

class KubefluxSchedEnv:
    """Gymnasium-compatible placement-aware scheduling environment.

    Action a ∈ {0 … 63}: schedule top-K job job_i on node node_j / GPU gpu_k.
    Action 64 (NO_OP)  : do nothing; simulator advances to the next event.
    """

    metadata = {"render_modes": []}

    def __init__(
        self,
        jobs_factory: Callable[[], List[Job]],
        *,
        n_nodes: int = N_NODES,
        gpus_per_node: int = N_GPUS,
        mps_per_gpu: int = MPS_PER_GPU,
        top_k: int = TOP_K,
        max_steps: int = 50_000,
        reward_mode: str = "jct_aligned",   # "jct_aligned" | "shaped"
        reward_scale: float = 1000.0,
        placement_reward_scale: float = 0.01,
    ) -> None:
        if gym is None:
            raise ImportError("gymnasium is not installed")
        if reward_mode not in ("jct_aligned", "shaped"):
            raise ValueError(f"reward_mode={reward_mode!r}")

        self.jobs_factory           = jobs_factory
        self.n_nodes                = n_nodes
        self.gpus_per_node          = gpus_per_node
        self.mps_per_gpu            = mps_per_gpu
        self.top_k                  = top_k
        self.max_steps              = max_steps
        self.reward_mode            = reward_mode
        self.reward_scale           = float(reward_scale)
        self.placement_reward_scale = float(placement_reward_scale)
        self.reward_betas: tuple    = (1.0, 0.0)   # (β_jct, β_slowdown)

        self._step_count = 0
        self._state: Optional[_RunState] = None

        n_act = top_k * n_nodes * gpus_per_node + 1
        obs_d = (top_k * JOB_FEAT_DIM
                 + n_nodes * gpus_per_node * GPU_FEAT_DIM
                 + TOPO_FEAT_DIM
                 + GLOBAL_FEAT_DIM)
        self.observation_space = spaces.Box(
            low=-np.inf, high=np.inf, shape=(obs_d,), dtype=np.float32,
        )
        self.action_space = spaces.Discrete(n_act)
        self._n_placements = n_nodes * gpus_per_node
        self._no_op        = n_act - 1

    # ── Gym API ──────────────────────────────────────────────────────────

    def reset(self, *, seed=None, options=None):
        if seed is not None:
            np.random.seed(seed)
        jobs    = self.jobs_factory()
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
            cluster=cluster, pending=[], events=events, seq=seq,
            now=0.0, by_id={j.job_id: j for j in jobs},
            completed=0, n_jobs=len(jobs),
            jct_sum=0.0, completion_reward=0.0,
        )
        self._step_count = 0
        self._advance_to_decision()
        return self._build_obs(), {}

    def step(self, action: int):
        if self._state is None:
            raise RuntimeError("call reset() first")
        st = self._state
        self._step_count += 1

        top        = self._top_k_jobs()
        r_place    = 0.0
        scheduled  = False

        if action != self._no_op:
            job_i, node_j, gpu_k = self._decode(action)
            if job_i < len(top):
                chosen = top[job_i]
                plan   = st.cluster.try_allocate_on(chosen, node_j, gpu_k)
                if plan is not None:
                    st.pending.remove(chosen)
                    end_ts = st.now + chosen.runtime
                    heapq.heappush(st.events, (end_ts, st.seq, "end", chosen.job_id))
                    st.seq += 1
                    scheduled = True
                    r_place   = self._placement_reward(chosen, node_j, gpu_k, st.cluster)
                else:
                    r_place = -0.01   # infeasible pick
            else:
                r_place = -0.01

        st.completion_reward = 0.0
        if not scheduled and st.events:
            t, _s, kind, payload = heapq.heappop(st.events)
            st.now = t
            if kind == "submit":
                st.pending.append(st.by_id[payload])
            elif kind == "end":
                self._on_job_end(payload)

        self._advance_to_decision()

        terminated = (st.completed >= st.n_jobs) and not st.events
        truncated  = self._step_count >= self.max_steps

        end_charge = 0.0
        if terminated or truncated:
            for j in st.pending:
                end_charge -= (st.now - j.submit_ts) / self.reward_scale

        reward = r_place + st.completion_reward + end_charge
        obs    = self._build_obs()
        info   = {
            "now": st.now, "queue_len": len(st.pending),
            "completed": st.completed, "n_jobs": st.n_jobs,
            "jct_sum": st.jct_sum,
            "avg_jct": st.jct_sum / max(1, st.completed) if st.completed else float("nan"),
        }
        return obs, float(reward), bool(terminated), bool(truncated), info

    # ── Action mask ──────────────────────────────────────────────────────

    def action_masks(self) -> np.ndarray:
        return self.action_mask()

    def action_mask(self) -> np.ndarray:
        """Bool mask over Discrete(N_ACTIONS). True = legal."""
        st   = self._state
        assert st is not None
        top  = self._top_k_jobs()
        mask = np.zeros(self.action_space.n, dtype=bool)
        for i, job in enumerate(top):
            for nj in range(self.n_nodes):
                for gk in range(self.gpus_per_node):
                    if st.cluster.can_allocate_on(job, nj, gk):
                        mask[encode_action(i, nj, gk)] = True
        mask[self._no_op] = True
        return mask

    # ── Internals ────────────────────────────────────────────────────────

    def _decode(self, a: int) -> Tuple[int, int, int]:
        job_i  = a // self._n_placements
        rem    = a %  self._n_placements
        node_j = rem // self.gpus_per_node
        gpu_k  = rem %  self.gpus_per_node
        return job_i, node_j, gpu_k

    def _top_k_jobs(self) -> List[Job]:
        st = self._state
        assert st is not None
        return sorted(st.pending, key=lambda j: j.submit_ts)[: self.top_k]

    def _advance_to_decision(self) -> None:
        st = self._state
        assert st is not None
        while st.events:
            if st.pending and any(
                st.cluster.can_allocate(j) for j in st.pending
            ):
                return
            t, _s, kind, payload = heapq.heappop(st.events)
            st.now = t
            if kind == "submit":
                st.pending.append(st.by_id[payload])
            elif kind == "end":
                self._on_job_end(payload)

    def _on_job_end(self, jid: str) -> None:
        st = self._state
        assert st is not None
        st.cluster.release(jid)
        st.completed += 1
        j   = st.by_id[jid]
        jct = st.now - j.submit_ts
        st.jct_sum += jct
        if self.reward_mode == "shaped":
            b_jct, b_slow = self.reward_betas
            runtime  = max(1.0, j.runtime)
            slowdown = max(1.0, jct / runtime)
            st.completion_reward += (b_jct * (-jct / self.reward_scale)
                                     + b_slow * (-math.log(slowdown)))
        else:  # jct_aligned
            st.completion_reward += -jct / self.reward_scale

    def _placement_reward(
        self, job: Job, node_j: int, gpu_k: int, cluster: Cluster
    ) -> float:
        """Dense reward shaping from score function factors (scaled small)."""
        total = cluster.mps_per_gpu
        gpu   = cluster.nodes[node_j].gpus[gpu_k]
        # f_mps_fit: prefer GPU where MPS utilization is high after placement
        remaining = gpu.free_mps - job.mps_req
        mps_fit   = 1.0 - remaining / total  # higher = tighter fit
        # f_fragmentation: penalise uneven free MPS across all GPUs
        free_all = [
            cluster.nodes[ni].gpus[gi].free_mps
            for ni in range(cluster.n_nodes)
            for gi in range(cluster.gpus_per_node)
        ]
        mean = max(1.0, sum(free_all) / len(free_all))
        var  = sum((x - mean) ** 2 for x in free_all) / len(free_all)
        frag = math.sqrt(var) / mean
        r = (0.4 * mps_fit - 0.2 * frag)
        return r * self.placement_reward_scale

    def _build_obs(self) -> np.ndarray:
        st = self._state
        assert st is not None
        top = self._top_k_jobs()

        job_feats = []
        for i in range(self.top_k):
            if i < len(top):
                job_feats.append(_job_feat(top[i], st.now, self.mps_per_gpu))
            else:
                job_feats.append(np.zeros(JOB_FEAT_DIM, dtype=np.float32))

        gpu_feats = []
        for ni in range(self.n_nodes):
            for gi in range(self.gpus_per_node):
                if ni < st.cluster.n_nodes and gi < st.cluster.gpus_per_node:
                    gpu_feats.append(_gpu_feat(st.cluster, ni, gi))
                else:
                    gpu_feats.append(np.zeros(GPU_FEAT_DIM, dtype=np.float32))

        topo   = _topo_feat(st.pending, st.cluster)
        glob   = _global_feat(st.pending, st.cluster, st.now)

        return np.concatenate([*job_feats, *gpu_feats, topo, glob]).astype(np.float32)

    def render(self):
        return None

    def close(self):
        self._state = None


# Register gymnasium subclass if gymnasium is available
if gym is not None:
    class KubefluxSchedGymEnv(KubefluxSchedEnv, gym.Env):  # type: ignore
        pass
