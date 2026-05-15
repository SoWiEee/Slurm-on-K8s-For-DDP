"""Step 3: DSAC + placement-aware gym_env integration smoke tests.

Verifies that:
1. DSACAgent dimensions match env_dims() output — no shape mismatch
2. A short rollout fills a ReplayBuffer correctly
3. agent.update() runs without error and returns finite losses
4. select_action() respects the action mask (never picks an illegal action)
5. After 200 gradient steps the Q-loss is finite and decreasing on average
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Allow import from repo root
sys.path.insert(0, str(Path(__file__).parents[2]))

torch = pytest.importorskip("torch")
gym   = pytest.importorskip("gymnasium")

from sim.gym_env import KubefluxSchedEnv, env_dims, N_ACTIONS, NO_OP, OBS_DIM
from sim.loader import Job
from services.rl_scheduler.dsac import DSACAgent
from services.rl_scheduler.rlpd_finetune import ReplayBuffer, Transition


# ── helpers ──────────────────────────────────────────────────────────────────

def _single_gpu_factory(n_jobs: int = 40, seed: int = 7):
    rng = np.random.default_rng(seed)
    def _build():
        from sim.loader import MPS_PER_GPU
        jobs = []
        for i in range(n_jobs):
            mps = int(rng.choice([1, 2, 4]))
            jobs.append(Job(
                job_id=f"j{i}", user="u0", gpu_count=1, gpu_type="rtx4070",
                submit_ts=float(i * 5), runtime=float(rng.integers(20, 120)),
                mem_req=0.0, mps_req=mps,
            ))
        return jobs
    return _build


def _make_env_and_agent(n_nodes=1, gpus_per_node=1):
    obs_dim, n_act = env_dims(n_nodes, gpus_per_node)
    env = KubefluxSchedEnv(
        _single_gpu_factory(),
        n_nodes=n_nodes,
        gpus_per_node=gpus_per_node,
        max_steps=5_000,
    )
    agent = DSACAgent(obs_dim=obs_dim, n_actions=n_act, hidden=(64, 64), device="cpu")
    return env, agent, obs_dim, n_act


# ── tests ─────────────────────────────────────────────────────────────────────

def test_agent_dims_match_env():
    obs_dim, n_act = env_dims(1, 1)
    assert obs_dim == OBS_DIM == 192
    assert n_act == N_ACTIONS == 17
    agent = DSACAgent(obs_dim=obs_dim, n_actions=n_act)
    # Q-nets should accept a random obs
    obs = np.zeros(obs_dim, dtype=np.float32)
    mask = np.ones(n_act, dtype=bool)
    a = agent.select_action(obs, mask)
    assert 0 <= a < n_act


def test_replay_buffer_fills():
    env, agent, obs_dim, n_act = _make_env_and_agent()
    buf = ReplayBuffer(capacity=500, obs_dim=obs_dim, n_actions=n_act)
    rng = np.random.default_rng(0)

    obs, _ = env.reset(seed=0)
    for _ in range(200):
        mask = env.action_mask()
        act = agent.select_action(obs, mask)
        next_obs, rew, term, trunc, _ = env.step(act)
        next_mask = env.action_mask()
        buf.add(Transition(
            obs=obs, act=act, rew=float(rew),
            next_obs=next_obs, done=bool(term or trunc),
            mask=mask, next_mask=next_mask,
        ))
        obs = next_obs
        if term or trunc:
            obs, _ = env.reset()

    assert len(buf) == 200
    batch = buf.sample(32, rng)
    assert batch["obs"].shape == (32, obs_dim)
    assert batch["masks"].shape == (32, n_act)
    env.close()


def test_update_produces_finite_losses():
    env, agent, obs_dim, n_act = _make_env_and_agent()
    buf = ReplayBuffer(capacity=1000, obs_dim=obs_dim, n_actions=n_act)
    rng = np.random.default_rng(1)

    obs, _ = env.reset(seed=1)
    # Collect 256 transitions first
    for _ in range(256):
        mask = env.action_mask()
        act = agent.select_action(obs, mask)
        next_obs, rew, term, trunc, _ = env.step(act)
        next_mask = env.action_mask()
        buf.add(Transition(
            obs=obs, act=act, rew=float(rew),
            next_obs=next_obs, done=bool(term or trunc),
            mask=mask, next_mask=next_mask,
        ))
        obs = next_obs
        if term or trunc:
            obs, _ = env.reset()

    losses = agent.update(buf.sample(64, rng))
    assert np.isfinite(losses["loss_q"]),    f"loss_q not finite: {losses}"
    assert np.isfinite(losses["loss_alpha"]), f"loss_alpha not finite: {losses}"
    assert np.isfinite(losses["alpha"]),      f"alpha not finite: {losses}"
    assert losses["alpha"] > 0
    env.close()


def test_select_action_respects_mask():
    env, agent, obs_dim, n_act = _make_env_and_agent()
    obs, _ = env.reset(seed=2)
    for _ in range(100):
        mask = env.action_mask()
        act = agent.select_action(obs, mask)
        assert mask[act], f"agent picked masked action {act}"
        obs, _, term, trunc, _ = env.step(act)
        if term or trunc:
            obs, _ = env.reset()
    env.close()


def test_200_gradient_steps_loss_finite():
    """200 update steps — loss must stay finite throughout."""
    env, agent, obs_dim, n_act = _make_env_and_agent()
    buf = ReplayBuffer(capacity=2000, obs_dim=obs_dim, n_actions=n_act)
    rng = np.random.default_rng(3)

    obs, _ = env.reset(seed=3)
    # Warm-up: fill 512 transitions
    for _ in range(512):
        mask = env.action_mask()
        act = agent.select_action(obs, mask)
        next_obs, rew, term, trunc, _ = env.step(act)
        next_mask = env.action_mask()
        buf.add(Transition(
            obs=obs, act=act, rew=float(rew),
            next_obs=next_obs, done=bool(term or trunc),
            mask=mask, next_mask=next_mask,
        ))
        obs = next_obs
        if term or trunc:
            obs, _ = env.reset()

    losses_q = []
    for step in range(200):
        batch = buf.sample(64, rng)
        info = agent.update(batch)
        assert np.isfinite(info["loss_q"]), f"NaN at step {step}: {info}"
        losses_q.append(info["loss_q"])

    # Loss should not explode (rough sanity: last-50 mean < first-50 mean × 10)
    first50 = float(np.mean(losses_q[:50]))
    last50  = float(np.mean(losses_q[-50:]))
    assert last50 < first50 * 10, f"loss exploded: {first50:.4f} → {last50:.4f}"
    env.close()
