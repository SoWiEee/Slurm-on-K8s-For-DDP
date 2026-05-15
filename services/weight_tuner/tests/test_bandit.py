"""Unit tests for the weight-tuner bandits.

We don't touch the simulator here — the bandit is supposed to be
simulator-agnostic. Tests use a tiny synthetic linear reward so the
expected best arm is known and convergence can be asserted.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from weight_tuner.bandit import (  # noqa: E402
    LinUCBPolicy,
    RandomPolicy,
    UCB1Policy,
    train,
)


# ---------------------------------------------------------------------------
# UCB1
# ---------------------------------------------------------------------------
class TestUCB1:
    def test_tries_every_arm_at_least_once(self):
        arms = [(0.1,), (0.5,), (0.9,)]
        policy = UCB1Policy(arms)
        pulled = []
        for _ in range(len(arms)):
            a = policy.select()
            pulled.append(a)
            policy.update(a, None, 0.0)
        assert set(pulled) == set(arms)

    def test_converges_to_best_arm_on_static_reward(self):
        # Arm 1 has the highest true mean; UCB1 should pull it most.
        rewards = {(0.1,): 0.1, (0.5,): 0.9, (0.9,): 0.3}
        policy = UCB1Policy(list(rewards), c=0.5)

        def pull(arm, ctx):
            # Add tiny Gaussian-ish noise so ties don't dominate.
            import random
            return rewards[arm] + (random.Random(hash((arm, ctx))).random() - 0.5) * 0.05

        # 1 context (UCB1 ignores it), 200 rounds.
        train(policy, pull, contexts=[(0.0,)], n_rounds=200, rng_seed=1)
        pulls = policy.pulls()
        # Best arm should dominate (>= 50% of pulls after 200 rounds with c=0.5).
        best = max(pulls, key=pulls.get)
        assert best == (0.5,)
        assert pulls[best] >= 100, f"pulls={pulls}"


# ---------------------------------------------------------------------------
# LinUCB
# ---------------------------------------------------------------------------
class TestLinUCB:
    def test_select_requires_correct_context_dim(self):
        policy = LinUCBPolicy(arms=[(0.0,), (1.0,)], d=3, alpha=1.0)
        with pytest.raises(ValueError):
            policy.select([0.5, 0.5])  # only 2 dims

    def test_learns_context_dependent_best_arm(self):
        """Two arms, two-dim context. Arm 'A' is best when ctx[0] is high,
        Arm 'B' when ctx[1] is high. LinUCB should adapt to context."""
        arms = [("A",), ("B",)]
        policy = LinUCBPolicy(arms=arms, d=2, alpha=0.4, ridge=1.0)

        def true_reward(arm, ctx):
            if arm == ("A",):
                return 1.0 * ctx[0] + 0.0 * ctx[1]
            return 0.0 * ctx[0] + 1.0 * ctx[1]

        # Two extreme contexts, alternating.
        ctxs = [(1.0, 0.0), (0.0, 1.0)]
        result = train(policy, true_reward, contexts=ctxs, n_rounds=200, rng_seed=2)

        # After training, on the A-favouring context the policy should
        # pick A almost always, and B on the B-context.
        last_50_A = [obs.arm for obs in result.history[-50:] if obs.context == (1.0, 0.0)]
        last_50_B = [obs.arm for obs in result.history[-50:] if obs.context == (0.0, 1.0)]
        # At least 80% correct in each bucket.
        if last_50_A:
            assert last_50_A.count(("A",)) / len(last_50_A) >= 0.8
        if last_50_B:
            assert last_50_B.count(("B",)) / len(last_50_B) >= 0.8

    def test_update_invalidates_theta_cache(self):
        policy = LinUCBPolicy(arms=[(0.0,)], d=2, alpha=1.0)
        arm = (0.0,)
        # First call caches theta=[0,0]
        assert policy.predict(arm, (1.0, 1.0)) == 0.0
        policy.update(arm, (1.0, 0.0), 5.0)
        # After update, theta should no longer be zero.
        pred = policy.predict(arm, (1.0, 0.0))
        assert pred > 0.0


# ---------------------------------------------------------------------------
# Cross-policy regret check
# ---------------------------------------------------------------------------
class TestRegret:
    def test_linucb_beats_random_on_context_dependent_problem(self):
        arms = [("A",), ("B",)]

        def reward(arm, ctx):
            if arm == ("A",):
                return 1.0 if ctx[0] > 0.5 else -0.5
            return 1.0 if ctx[1] > 0.5 else -0.5

        ctxs = [(1.0, 0.0), (0.0, 1.0)]
        rng_seed = 3
        rnd = RandomPolicy(arms, seed=rng_seed)
        lin = LinUCBPolicy(arms, d=2, alpha=0.4)

        r_rnd = train(rnd, reward, contexts=ctxs, n_rounds=200, rng_seed=rng_seed)
        r_lin = train(lin, reward, contexts=ctxs, n_rounds=200, rng_seed=rng_seed)

        total_rnd = sum(o.reward for o in r_rnd.history)
        total_lin = sum(o.reward for o in r_lin.history)
        # LinUCB should accumulate clearly more reward.
        assert total_lin > total_rnd + 20, f"lin={total_lin} rnd={total_rnd}"
