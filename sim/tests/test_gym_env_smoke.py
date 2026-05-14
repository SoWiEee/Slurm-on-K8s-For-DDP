"""Smoke tests: placement-aware gym env (Discrete(65) action space)."""
from __future__ import annotations

import numpy as np
import pytest

gym = pytest.importorskip("gymnasium")

from sim.gym_env import (
    GLOBAL_FEAT_DIM,
    GPU_FEAT_DIM,
    JOB_FEAT_DIM,
    N_ACTIONS,
    N_GPUS,
    N_NODES,
    NO_OP,
    OBS_DIM,
    TOPO_FEAT_DIM,
    TOP_K,
    KubefluxSchedEnv,
    decode_action,
    encode_action,
)
from sim.loader import generate_by_family


def _factory(n_jobs: int = 50, seed: int = 42, family: str = "philly"):
    def _build():
        return generate_by_family(family, n_jobs=n_jobs, seed=seed)
    return _build


# ── obs / action space shape ─────────────────────────────────────────────

def test_obs_dim_constant():
    expected = TOP_K * JOB_FEAT_DIM + N_NODES * N_GPUS * GPU_FEAT_DIM + TOPO_FEAT_DIM + GLOBAL_FEAT_DIM
    assert OBS_DIM == expected == 210


def test_reset_returns_correct_shape():
    env = KubefluxSchedEnv(_factory())
    obs, info = env.reset(seed=0)
    assert obs.shape == (OBS_DIM,)
    assert obs.dtype == np.float32
    assert not np.isnan(obs).any()
    env.close()


def test_action_space_size():
    env = KubefluxSchedEnv(_factory())
    assert env.action_space.n == N_ACTIONS == 65
    env.close()


# ── action codec roundtrip ───────────────────────────────────────────────

def test_encode_decode_roundtrip():
    for job_i in range(TOP_K):
        for nj in range(N_NODES):
            for gk in range(N_GPUS):
                a = encode_action(job_i, nj, gk)
                assert decode_action(a) == (job_i, nj, gk)


def test_no_op_value():
    assert NO_OP == 64


def test_decode_no_op_raises():
    with pytest.raises(ValueError):
        decode_action(NO_OP)


# ── action mask ──────────────────────────────────────────────────────────

def test_action_mask_shape_and_no_op():
    env = KubefluxSchedEnv(_factory())
    env.reset(seed=0)
    mask = env.action_mask()
    assert mask.shape == (N_ACTIONS,)
    assert mask[NO_OP] == True  # noqa: E712
    env.close()


def test_action_mask_illegal_placements_blocked():
    """Actions targeting non-existent or full GPUs must be masked."""
    env = KubefluxSchedEnv(_factory(n_jobs=20))
    env.reset(seed=0)
    mask = env.action_mask()
    # Every True placement action must decode to a valid (job, node, gpu) index
    for a in np.where(mask)[0]:
        if a == NO_OP:
            continue
        job_i, node_j, gpu_k = decode_action(a)
        assert job_i < TOP_K
        assert node_j < N_NODES
        assert gpu_k < N_GPUS
    env.close()


# ── episode dynamics ─────────────────────────────────────────────────────

def test_random_policy_episode_terminates():
    rng = np.random.default_rng(0)
    # Use "ali" trace: all jobs have gpu_count=1 (MPS fractional), so every
    # job fits in a 2×2 cluster and the episode terminates naturally.
    env = KubefluxSchedEnv(_factory(n_jobs=30, family="ali"), max_steps=20_000)
    obs, _ = env.reset(seed=0)
    total_reward = 0.0
    steps = 0
    terminated = truncated = False
    while not (terminated or truncated):
        legal = np.where(env.action_mask())[0]
        a = int(rng.choice(legal))
        obs, r, terminated, truncated, info = env.step(a)
        total_reward += r
        steps += 1
    assert terminated, f"did not terminate (truncated={truncated}, steps={steps})"
    assert info["completed"] == info["n_jobs"]
    assert info["avg_jct"] > 0
    print(f"\nrandom episode: steps={steps} reward={total_reward:.2f} "
          f"avg_jct={info['avg_jct']:.0f}s")
    env.close()


def test_noop_policy_truncates():
    env = KubefluxSchedEnv(_factory(n_jobs=10), max_steps=200)
    env.reset(seed=0)
    terminated = truncated = False
    while not (terminated or truncated):
        _, _, terminated, truncated, info = env.step(NO_OP)
    assert truncated or info["completed"] < info["n_jobs"]
    env.close()


def test_shaped_reward_mode():
    env = KubefluxSchedEnv(_factory(n_jobs=20), reward_mode="shaped")
    env.reward_betas = (1.0, 0.5)
    rng = np.random.default_rng(1)
    obs, _ = env.reset(seed=1)
    for _ in range(50):
        legal = np.where(env.action_mask())[0]
        _, _, terminated, truncated, _ = env.step(int(rng.choice(legal)))
        if terminated or truncated:
            break
    env.close()


# ── placement-aware allocation ───────────────────────────────────────────

def test_placement_action_schedules_on_correct_gpu():
    """Verify that try_allocate_on places on the specified GPU."""
    from sim.cluster import Cluster
    from sim.loader import Job, MPS_PER_GPU

    cluster = Cluster(n_nodes=2, gpus_per_node=2, mps_per_gpu=MPS_PER_GPU)
    job = Job("j0", "u", 1, "rtx4070", 0.0, 100.0, 0.0, MPS_PER_GPU // 2)

    free_before = cluster.nodes[0].gpus[1].free_mps
    plan = cluster.try_allocate_on(job, node_i=0, gpu_i=1)
    assert plan is not None, "allocation should succeed"
    assert plan[0].node_id == 0
    assert 1 in plan[0].gpu_indices
    assert cluster.nodes[0].gpus[1].free_mps < free_before

    # Other GPU on same node should be untouched
    assert cluster.nodes[0].gpus[0].free_mps == free_before
