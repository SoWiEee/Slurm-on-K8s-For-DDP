from __future__ import annotations

import numpy as np

from services.rl_scheduler.rlpd_finetune import (
    PrioritizedReplayBuffer,
    ReplayBuffer,
    Transition,
)


def _transition(value: float, *, obs_dim: int = 4, n_actions: int = 3) -> Transition:
    obs = np.full((obs_dim,), value, dtype=np.float32)
    next_obs = obs + 1
    mask = np.array([True, False, True], dtype=np.bool_)[:n_actions]
    next_mask = np.ones((n_actions,), dtype=np.bool_)
    return Transition(
        obs=obs,
        act=int(value) % n_actions,
        rew=value,
        next_obs=next_obs,
        done=False,
        mask=mask,
        next_mask=next_mask,
    )


def test_replay_buffer_fifo_wraparound_and_gamma_storage():
    buf = ReplayBuffer(capacity=2, obs_dim=4, n_actions=3)

    buf.add(_transition(1.0), gamma=0.9)
    buf.add(_transition(2.0), gamma=0.8)
    buf.add(_transition(3.0), gamma=0.7)

    assert len(buf) == 2
    assert buf._idx == 1
    assert np.allclose(buf.obs[0], np.full((4,), 3.0))
    assert np.allclose(buf.obs[1], np.full((4,), 2.0))
    assert np.isclose(buf.gammas[0], 0.7)
    assert np.isclose(buf.gammas[1], 0.8)

    batch = buf.sample(8, np.random.default_rng(123))
    assert batch["obs"].shape == (8, 4)
    assert batch["masks"].shape == (8, 3)
    assert set(batch.keys()) >= {"obs", "acts", "rews", "next_obs", "dones", "masks", "next_masks", "gammas"}


def test_prioritized_replay_sample_returns_indices_weights_and_updates_priorities():
    buf = PrioritizedReplayBuffer(capacity=4, obs_dim=4, n_actions=3)
    for value in range(4):
        buf.add(_transition(float(value)), gamma=0.95)

    rng = np.random.default_rng(7)
    batch = buf.sample(4, rng)

    assert batch["obs"].shape == (4, 4)
    assert batch["weights"].shape == (4,)
    assert batch["indices"].shape == (4,)
    assert np.all(batch["weights"] > 0)
    assert np.all(batch["weights"] <= 1.0)
    assert np.allclose(batch["gammas"], 0.95)

    old_total = buf._tree.total
    buf.update_priorities(batch["indices"], np.array([0.1, 1.0, 2.0, 4.0], dtype=np.float32))
    assert buf._tree.total != old_total
