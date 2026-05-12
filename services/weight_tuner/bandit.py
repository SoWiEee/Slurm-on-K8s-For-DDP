"""Contextual bandits for score-function weight tuning (Phase 6 M9).

This module implements two policies that share the same `BanditPolicy`
interface:

  * UCB1Policy    — single-context UCB (Auer et al. 2002), used as the
                    baseline. No context features; estimates one mean
                    reward per arm.
  * LinUCBPolicy  — contextual LinUCB (Li et al. WWW'10). Each arm has
                    its own ridge-regression head θ_a = A_a^-1 b_a and
                    we pull argmax over θ_a·x + α·sqrt(x·A_a^-1·x).

The "action" is a tuple of score-function weights — typically
(alpha, beta, delta, epsilon). The "reward" is the negative of mean
JCT in hours (higher = better), so the bandit is maximising.

The bandit owns neither the simulator nor the arm set; callers pass an
explicit `arms` list and call `select(context)` then `update(arm,
context, reward)` per pull. Keeping it simulator-agnostic makes the
unit tests trivial (linear synthetic reward) and lets the same code
later drive a Slurm-on-K8s live evaluator without changes.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Hashable, List, Optional, Sequence, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------
Arm = Tuple[float, ...]


@dataclass
class BanditObservation:
    """One round's data — used for replay / regret bookkeeping."""

    arm: Arm
    context: Tuple[float, ...]
    reward: float


class BanditPolicy:
    """Common interface so the runner can swap policies."""

    name: str = "abstract"

    def select(self, context: Sequence[float], rng: Optional[random.Random] = None) -> Arm:
        raise NotImplementedError

    def update(self, arm: Arm, context: Sequence[float], reward: float) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Baseline policies
# ---------------------------------------------------------------------------
class RandomPolicy(BanditPolicy):
    """Pulls a uniformly random arm. Used as the regret lower bound."""

    name = "random"

    def __init__(self, arms: Sequence[Arm], seed: int = 0):
        self.arms = list(arms)
        self._rng = random.Random(seed)

    def select(self, context, rng=None):
        return (rng or self._rng).choice(self.arms)

    def update(self, arm, context, reward):
        pass


class UCB1Policy(BanditPolicy):
    """UCB1 — context-free upper-confidence-bound bandit.

    Reward bookkeeping:
      n_a       times arm a has been pulled
      mean_a    running mean reward
      total_t   total pulls across all arms

    UCB index: mean_a + c · sqrt(2·ln(total_t)/n_a). Untried arms have
    +∞ index so they're tried first in arbitrary order.
    """

    name = "ucb1"

    def __init__(self, arms: Sequence[Arm], c: float = 1.0):
        self.arms: List[Arm] = list(arms)
        self.c = c
        self._n = {a: 0 for a in self.arms}
        self._mean = {a: 0.0 for a in self.arms}
        self._t = 0

    def select(self, context=None, rng=None):
        # Try every arm at least once first (classic UCB1 init).
        for a in self.arms:
            if self._n[a] == 0:
                return a
        log_t = math.log(max(self._t, 1))
        best_arm = None
        best_score = -math.inf
        for a in self.arms:
            bonus = self.c * math.sqrt(2.0 * log_t / self._n[a])
            score = self._mean[a] + bonus
            if score > best_score:
                best_score = score
                best_arm = a
        return best_arm  # type: ignore[return-value]

    def update(self, arm, context, reward):
        self._t += 1
        self._n[arm] += 1
        n = self._n[arm]
        # Online mean update.
        self._mean[arm] += (reward - self._mean[arm]) / n

    # Useful for tests / writeup
    def best_arm(self) -> Arm:
        return max(self.arms, key=lambda a: self._mean[a])

    def pulls(self) -> dict:
        return dict(self._n)


# ---------------------------------------------------------------------------
# LinUCB — disjoint per-arm linear model with UCB exploration
# ---------------------------------------------------------------------------
class LinUCBPolicy(BanditPolicy):
    """LinUCB (Li et al. 2010), disjoint variant.

    Each arm a has:
      A_a      d×d ridge regression matrix, init λ·I
      b_a      d-vector of accumulated x·r
      θ_a      A_a^{-1} b_a  (recomputed lazily)

    Selection: argmax_a  θ_a·x + α · sqrt(x·A_a^{-1}·x).

    Parameters
    ----------
    arms     iterable of Arm tuples (anything hashable)
    d        context dimensionality
    alpha    exploration coefficient. Li et al. recommend
             alpha = 1 + sqrt(ln(2/delta)/2). 0.5–2.0 is a sensible
             range; larger = more exploration.
    ridge    ridge penalty λ. Default 1.0 — keeps A_a invertible from
             the first pull and gates the initial uncertainty term.
    """

    name = "linucb"

    def __init__(self, arms: Sequence[Arm], d: int, alpha: float = 1.0, ridge: float = 1.0):
        self.arms: List[Arm] = list(arms)
        self.d = d
        self.alpha = alpha
        self.ridge = ridge
        self._A = {a: ridge * np.eye(d) for a in self.arms}
        self._b = {a: np.zeros(d) for a in self.arms}
        # Cache for θ; invalidate on update.
        self._theta: dict[Arm, np.ndarray] = {}

    def _theta_for(self, arm: Arm) -> np.ndarray:
        th = self._theta.get(arm)
        if th is None:
            th = np.linalg.solve(self._A[arm], self._b[arm])
            self._theta[arm] = th
        return th

    def select(self, context, rng=None):
        x = np.asarray(context, dtype=float)
        if x.shape != (self.d,):
            raise ValueError(f"context dim mismatch: got {x.shape}, expected ({self.d},)")
        best_arm = None
        best_score = -math.inf
        for a in self.arms:
            A_inv_x = np.linalg.solve(self._A[a], x)
            ucb = self.alpha * math.sqrt(max(float(x @ A_inv_x), 0.0))
            mean_pred = float(self._theta_for(a) @ x)
            score = mean_pred + ucb
            if score > best_score:
                best_score = score
                best_arm = a
        return best_arm  # type: ignore[return-value]

    def update(self, arm, context, reward):
        x = np.asarray(context, dtype=float)
        self._A[arm] = self._A[arm] + np.outer(x, x)
        self._b[arm] = self._b[arm] + reward * x
        # θ for this arm is stale; clear cache for it
        self._theta.pop(arm, None)

    # Convenience for the writeup
    def predict(self, arm: Arm, context: Sequence[float]) -> float:
        x = np.asarray(context, dtype=float)
        return float(self._theta_for(arm) @ x)


# ---------------------------------------------------------------------------
# Trainer / regret bookkeeping
# ---------------------------------------------------------------------------
@dataclass
class TrainResult:
    """Per-round records + summary stats. Returned by `train`."""

    policy: BanditPolicy
    history: List[BanditObservation] = field(default_factory=list)
    best_arm_at_each_round: List[Arm] = field(default_factory=list)

    def cumulative_reward(self) -> List[float]:
        s = 0.0
        out = []
        for obs in self.history:
            s += obs.reward
            out.append(s)
        return out

    def cumulative_regret(self, oracle_reward_per_round: List[float]) -> List[float]:
        assert len(oracle_reward_per_round) == len(self.history)
        s = 0.0
        out = []
        for obs, oracle in zip(self.history, oracle_reward_per_round):
            s += (oracle - obs.reward)
            out.append(s)
        return out


def train(
    policy: BanditPolicy,
    pull: callable,
    contexts: Sequence[Sequence[float]],
    n_rounds: int,
    rng_seed: int = 0,
) -> TrainResult:
    """Run `n_rounds` of bandit interaction.

    `pull(arm, context) -> reward` is the environment. `contexts` is a
    pool of contexts to sample uniformly each round (typically one
    context per (trace, seed) combo).
    """
    rng = random.Random(rng_seed)
    result = TrainResult(policy=policy)
    for _ in range(n_rounds):
        ctx = rng.choice(list(contexts))
        arm = policy.select(ctx, rng=rng)
        reward = pull(arm, ctx)
        policy.update(arm, ctx, reward)
        result.history.append(BanditObservation(arm=arm, context=tuple(ctx), reward=reward))
    return result
