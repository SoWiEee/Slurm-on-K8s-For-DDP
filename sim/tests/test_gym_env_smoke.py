"""M11 Phase A smoke tests: gym env reset/step + random policy episode."""
from __future__ import annotations

import numpy as np
import pytest

gym = pytest.importorskip("gymnasium")

from sim.gym_env import (
    GLOBAL_FEAT_DIM,
    JOB_FEAT_DIM,
    KubefluxSchedEnv,
    MAX_NODES,
    NODE_FEAT_DIM,
    TOP_K,
)
from sim.loader import generate_by_family


def _factory(n_jobs: int = 50, seed: int = 42, family: str = "philly"):
    def _build():
        return generate_by_family(family, n_jobs=n_jobs, seed=seed)
    return _build


def test_reset_returns_correct_shape():
    env = KubefluxSchedEnv(_factory(), n_nodes=2, gpus_per_node=1)
    obs, info = env.reset(seed=0)
    expected = TOP_K * JOB_FEAT_DIM + MAX_NODES * NODE_FEAT_DIM + GLOBAL_FEAT_DIM
    assert obs.shape == (expected,)
    assert obs.dtype == np.float32
    assert not np.isnan(obs).any()
    env.close()


def test_action_space_discrete_k_plus_one():
    env = KubefluxSchedEnv(_factory(), n_nodes=2, gpus_per_node=1)
    assert env.action_space.n == TOP_K + 1
    env.close()


def test_action_mask_no_op_always_legal():
    env = KubefluxSchedEnv(_factory(), n_nodes=2, gpus_per_node=1)
    env.reset(seed=0)
    mask = env.action_mask()
    assert mask.shape == (TOP_K + 1,)
    assert mask[TOP_K] == True  # noqa: E712
    env.close()


def test_random_policy_episode_terminates():
    rng = np.random.default_rng(0)
    # Use 4-node × 4-GPU cluster (sim default) so 4-GPU and 8-GPU jobs fit
    env = KubefluxSchedEnv(_factory(n_jobs=30), n_nodes=4, gpus_per_node=4, max_steps=10_000)
    obs, _ = env.reset(seed=0)
    total_reward = 0.0
    steps = 0
    terminated = truncated = False
    while not (terminated or truncated):
        mask = env.action_mask()
        # Random legal action
        legal = np.where(mask)[0]
        a = int(rng.choice(legal))
        obs, r, terminated, truncated, info = env.step(a)
        total_reward += r
        steps += 1
    assert terminated, f"episode did not terminate naturally (truncated={truncated}, steps={steps})"
    assert info["completed"] == info["n_jobs"]
    assert info["avg_jct"] > 0
    print(f"\nrandom-policy episode: steps={steps}, total_reward={total_reward:.2f}, "
          f"avg_jct={info['avg_jct']:.2f}, n_jobs={info['n_jobs']}")
    env.close()


def test_noop_only_policy_eventually_completes():
    """Sanity: pure no-op never schedules anything — episode should NOT complete.
    Tests the env's truncation path, not natural completion."""
    env = KubefluxSchedEnv(_factory(n_jobs=10), n_nodes=2, gpus_per_node=1, max_steps=200)
    env.reset(seed=0)
    terminated = truncated = False
    while not (terminated or truncated):
        obs, r, terminated, truncated, info = env.step(TOP_K)  # always no-op
    # With pure no-op, no jobs get scheduled, so events drain (only submits),
    # then events is empty + completed=0 < n_jobs → neither terminated nor truncated until max_steps.
    # The env's terminated condition needs `completed >= n_jobs` AND `events empty`. Since we never
    # schedule, completed stays 0 → terminated is False. Truncated when step_count >= max_steps.
    assert truncated or info["completed"] < info["n_jobs"]
    env.close()
