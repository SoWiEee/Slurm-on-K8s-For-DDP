"""Hierarchical scheduler: D-LinUCB outer loop + DSAC inner loop.

D-LinUCB (Russac et al., NeurIPS 2019) selects reward-shaping coefficients
(β_jct, β_slowdown) that determine the reward function the inner DSAC
optimises. After each inner training round, the realised mean JCT is fed
back to the bandit as reward.

Architecture:
  Outer (D-LinUCB, 5-dim context, hour-scale):
    arms   = 9 discrete (β_jct, β_slow) pairs on 3×3 grid
    reward = -mean_JCT_hours after inner training

  Inner (DSAC, per-decision):
    reward = β_jct * (-jct/scale) + β_slow * (-log_slowdown)
    uses sim offline buffer + any available live transitions
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

from sim.gym_env import (
    KubefluxSchedGymEnv, _global_feat, GLOBAL_FEAT_DIM,
)
from sim.loader import generate_by_family
from .dsac import DSACAgent
from .rlpd_finetune import (
    ReplayBuffer, Transition, collect_sim_rollouts, mixed_batch,
)


# ---------------------------------------------------------------------------
# D-LinUCB
# ---------------------------------------------------------------------------
@dataclass
class RewardShapingArm:
    beta_jct: float
    beta_slowdown: float

    @property
    def reward_betas(self) -> tuple[float, float]:
        return (self.beta_jct, self.beta_slowdown)

    def __repr__(self) -> str:
        return f"Arm(β_jct={self.beta_jct:.1f}, β_slow={self.beta_slowdown:.1f})"


class DLinUCB:
    """Discounted Linear UCB bandit (Russac et al. NeurIPS 2019).

    Maintains separate (A_a, b_a) for each arm, discounted by γ on each
    update so older observations contribute less.

    select(context) → arm index
    update(arm_idx, context, reward)
    """

    def __init__(
        self,
        n_arms: int,
        context_dim: int = GLOBAL_FEAT_DIM,
        alpha: float = 1.0,
        ridge: float = 1.0,
        gamma: float = 0.99,
    ) -> None:
        self.n_arms = n_arms
        self.context_dim = context_dim
        self.alpha = alpha
        self.gamma = gamma
        d = context_dim
        # A_a initialised to ridge*I; b_a to zero
        self.A = [ridge * np.eye(d) for _ in range(n_arms)]
        self.b = [np.zeros(d) for _ in range(n_arms)]
        self._t = 0

    def _theta(self, arm_idx: int) -> np.ndarray:
        try:
            return np.linalg.solve(self.A[arm_idx], self.b[arm_idx])
        except np.linalg.LinAlgError:
            return np.zeros(self.context_dim)

    def _ucb_score(self, arm_idx: int, context: np.ndarray) -> float:
        theta = self._theta(arm_idx)
        exploit = float(theta @ context)
        try:
            A_inv = np.linalg.inv(self.A[arm_idx])
        except np.linalg.LinAlgError:
            A_inv = np.eye(self.context_dim)
        explore = self.alpha * math.sqrt(float(context @ A_inv @ context))
        return exploit + explore

    def select(self, context: np.ndarray) -> int:
        scores = [self._ucb_score(i, context) for i in range(self.n_arms)]
        return int(np.argmax(scores))

    def update(self, arm_idx: int, context: np.ndarray, reward: float) -> None:
        x = context.astype(np.float64)
        # Discount existing statistics
        self.A[arm_idx] = self.gamma * self.A[arm_idx] + np.outer(x, x)
        self.b[arm_idx] = self.gamma * self.b[arm_idx] + reward * x
        self._t += 1

    def state_dict(self) -> dict:
        return {
            "A": [a.tolist() for a in self.A],
            "b": [b.tolist() for b in self.b],
            "t": self._t,
        }

    def load_state_dict(self, d: dict) -> None:
        self.A = [np.array(a) for a in d["A"]]
        self.b = [np.array(b) for b in d["b"]]
        self._t = d["t"]


# ---------------------------------------------------------------------------
# HierarchicalTrainer
# ---------------------------------------------------------------------------
def _build_arms() -> List[RewardShapingArm]:
    """3×3 grid: β_jct ∈ {0.5, 1.0, 2.0} × β_slow ∈ {0.0, 0.5, 1.0}."""
    arms = []
    for bj in (0.5, 1.0, 2.0):
        for bs in (0.0, 0.5, 1.0):
            arms.append(RewardShapingArm(beta_jct=bj, beta_slowdown=bs))
    return arms


def _eval_dsac(
    agent: DSACAgent,
    trace_family: str,
    n_jobs: int,
    n_nodes: int,
    gpus_per_node: int,
    seeds: tuple[int, ...] = (42, 43, 44),
) -> float:
    """Greedy rollout of DSAC, return mean avg_jct in seconds."""
    jcts = []
    for seed in seeds:
        env = KubefluxSchedGymEnv(
            jobs_factory=lambda _s=seed: generate_by_family(
                trace_family, n_jobs=n_jobs, seed=_s),
            n_nodes=n_nodes,
            gpus_per_node=gpus_per_node,
            max_steps=n_jobs * 100,
            reward_mode="jct_aligned",
        )
        obs, _ = env.reset()
        done = False
        info: dict = {}
        while not done:
            mask = env.action_masks()
            act = agent.select_action(obs, mask, greedy=True)
            obs, _, terminated, truncated, info = env.step(act)
            done = terminated or truncated
        env.close()
        jcts.append(info.get("avg_jct", float("nan")))
    return float(np.nanmean(jcts))


def _get_context(
    trace_family: str,
    n_jobs: int,
    n_nodes: int,
    gpus_per_node: int,
    seed: int = 42,
) -> np.ndarray:
    """Run env for a few steps to get a representative global context."""
    env = KubefluxSchedGymEnv(
        jobs_factory=lambda: generate_by_family(
            trace_family, n_jobs=min(n_jobs, 30), seed=seed),
        n_nodes=n_nodes,
        gpus_per_node=gpus_per_node,
        max_steps=50,
        reward_mode="jct_aligned",
    )
    obs, _ = env.reset()
    contexts = []
    for _ in range(min(20, n_jobs)):
        mask = env.action_masks()
        legal = np.flatnonzero(mask)
        act = int(legal[0]) if len(legal) else env.action_space.n - 1
        obs, _, term, trunc, _ = env.step(act)
        if env._state is not None:
            st = env._state
            ctx = _global_feat(st.pending, st.cluster, st.now)
            contexts.append(ctx)
        if term or trunc:
            break
    env.close()
    return np.mean(contexts, axis=0) if contexts else np.zeros(GLOBAL_FEAT_DIM)


class HierarchicalTrainer:
    """D-LinUCB outer loop + DSAC inner loop.

    Each outer round:
      1. D-LinUCB selects arm (β_jct, β_slow)
      2. Inner DSAC trains N_inner steps with shaped reward
      3. Eval DSAC → mean JCT → reward = -mean_JCT_hours
      4. D-LinUCB updated with (context, reward)
    """

    def __init__(
        self,
        *,
        trace_family: str = "philly",
        n_jobs: int = 200,
        n_nodes: int = 2,
        gpus_per_node: int = 2,
        n_outer: int = 5,
        n_inner: int = 2000,
        utd_ratio: int = 4,
        batch_size: int = 256,
        offline_buffer_size: int = 5000,
        seed: int = 42,
        device: str = "cpu",
        out_dir: str | Path = "runs/hierarchical",
        bandit_alpha: float = 1.0,
        bandit_gamma: float = 0.99,
    ) -> None:
        self.trace_family = trace_family
        self.n_jobs = n_jobs
        self.n_nodes = n_nodes
        self.gpus_per_node = gpus_per_node
        self.n_outer = n_outer
        self.n_inner = n_inner
        self.utd_ratio = utd_ratio
        self.batch_size = batch_size
        self.offline_buffer_size = offline_buffer_size
        self.seed = seed
        self.device = device
        self.out_dir = Path(out_dir)

        self.arms = _build_arms()
        self.bandit = DLinUCB(
            n_arms=len(self.arms),
            context_dim=GLOBAL_FEAT_DIM,
            alpha=bandit_alpha,
            gamma=bandit_gamma,
        )
        self._rng = np.random.default_rng(seed)

    # ------------------------------------------------------------------
    def _make_env(self, arm: RewardShapingArm, seed: int) -> KubefluxSchedGymEnv:
        env = KubefluxSchedGymEnv(
            jobs_factory=lambda _s=seed: generate_by_family(
                self.trace_family, n_jobs=self.n_jobs, seed=_s),
            n_nodes=self.n_nodes,
            gpus_per_node=self.gpus_per_node,
            max_steps=self.n_jobs * 100,
            reward_mode="shaped",
        )
        env.reward_betas = arm.reward_betas
        return env

    def _collect_inner(
        self,
        agent: DSACAgent,
        arm: RewardShapingArm,
        online_buf: ReplayBuffer,
        n_steps: int,
    ) -> None:
        """Collect n_steps transitions into online_buf using current agent."""
        env = self._make_env(arm, int(self._rng.integers(0, 10000)))
        obs, _ = env.reset()
        collected = 0
        while collected < n_steps:
            mask = env.action_masks()
            act = agent.select_action(obs, mask, greedy=False)
            next_obs, rew, term, trunc, _ = env.step(act)
            next_mask = env.action_masks()
            online_buf.add(Transition(
                obs=obs.astype(np.float32).reshape(-1),
                act=act, rew=float(rew),
                next_obs=next_obs.astype(np.float32).reshape(-1),
                done=bool(term or trunc),
                mask=mask,
                next_mask=next_mask,
            ))
            obs = next_obs
            collected += 1
            if term or trunc:
                s = int(self._rng.integers(0, 10000))
                env = self._make_env(arm, s)
                obs, _ = env.reset()
        env.close()

    # ------------------------------------------------------------------
    def train(self) -> dict:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.out_dir / "hierarchical_train.jsonl"

        # Shared DSAC agent across all outer rounds (continuous fine-tune)
        dsac_path = self.out_dir / "dsac.pt"
        if dsac_path.exists():
            print(f"[hier] warm-starting DSAC from {dsac_path}")
            agent = DSACAgent.load(dsac_path, device=self.device)
        else:
            agent = DSACAgent(obs_dim=193, n_actions=17, device=self.device)

        # Shared offline buffer (sim, diverse random policy)
        print(f"[hier] collecting offline buffer ({self.offline_buffer_size} steps)...")
        offline_buf = collect_sim_rollouts(
            n_transitions=self.offline_buffer_size,
            trace_family=self.trace_family,
            n_jobs=self.n_jobs,
            n_nodes=self.n_nodes,
            gpus_per_node=self.gpus_per_node,
            seed=self.seed,
        )
        obs_dim = offline_buf.obs_dim
        n_actions = offline_buf.n_actions

        # Shared online buffer (refilled each outer round)
        online_buf = ReplayBuffer(
            capacity=self.n_inner * self.n_outer * 2,
            obs_dim=obs_dim, n_actions=n_actions,
        )

        best_jct = float("inf")
        best_arm = self.arms[0]
        history = []

        with open(log_path, "w") as fh:
            for outer in range(self.n_outer):
                t0 = time.time()
                context = _get_context(
                    self.trace_family, self.n_jobs,
                    self.n_nodes, self.gpus_per_node,
                    seed=self.seed + outer,
                )
                arm_idx = self.bandit.select(context)
                arm = self.arms[arm_idx]
                print(f"\n[hier] outer {outer+1}/{self.n_outer}  "
                      f"arm={arm}  (UCB selected)")

                # Collect n_inner / 4 transitions for this round's online data
                collect_steps = max(self.batch_size, self.n_inner // 4)
                self._collect_inner(agent, arm, online_buf, collect_steps)

                # DSAC gradient updates
                loss_last: dict = {}
                for step in range(self.n_inner):
                    for _ in range(self.utd_ratio):
                        batch = mixed_batch(
                            offline=offline_buf, online=online_buf,
                            batch_size=self.batch_size,
                            online_ratio=0.5,
                            rng=self._rng,
                        )
                        loss_last = agent.update(batch)
                    if (step + 1) % 500 == 0:
                        print(f"  inner {step+1}/{self.n_inner}  "
                              f"loss_q={loss_last.get('loss_q', 0):.4f}  "
                              f"alpha={loss_last.get('alpha', 0):.4f}")

                # Evaluate
                jct_s = _eval_dsac(
                    agent, self.trace_family, self.n_jobs,
                    self.n_nodes, self.gpus_per_node,
                )
                reward = -jct_s / 3600.0
                self.bandit.update(arm_idx, context, reward)

                elapsed = time.time() - t0
                row = {
                    "outer": outer, "arm": arm_idx,
                    "beta_jct": arm.beta_jct, "beta_slowdown": arm.beta_slowdown,
                    "jct_s": jct_s, "reward": reward,
                    "context": context.tolist(),
                    "elapsed_s": elapsed, **loss_last,
                }
                fh.write(json.dumps(row) + "\n")
                print(f"  JCT={jct_s/3600:.3f}h  reward={reward:.4f}  "
                      f"elapsed={elapsed:.0f}s")

                if jct_s < best_jct:
                    best_jct = jct_s
                    best_arm = arm
                    agent.save(dsac_path)
                    print(f"  *** new best — dsac.pt updated ***")

                history.append(row)

        # Final comparison table
        from sim.runner import run as sim_run
        score_jcts = []
        for s in (42, 43, 44):
            jobs = generate_by_family(self.trace_family, n_jobs=self.n_jobs, seed=s)
            m, _ = sim_run(jobs, n_nodes=self.n_nodes,
                           gpus_per_node=self.gpus_per_node,
                           scheduler_name="score")
            score_jcts.append(m.summary()["jct_mean"])
        score_mean = float(np.mean(score_jcts))
        pct = (score_mean - best_jct) / score_mean * 100

        print(f"\n{'='*50}")
        print(f"Hierarchical DSAC best JCT : {best_jct/3600:.3f}h ({best_arm})")
        print(f"Score baseline             : {score_mean/3600:.3f}h")
        print(f"Δ(score − hier)            : {pct:+.1f}%")
        print(f"Bandit state saved: {self.out_dir / 'bandit.json'}")
        with open(self.out_dir / "bandit.json", "w") as f:
            json.dump(self.bandit.state_dict(), f, indent=2)

        return {
            "best_arm": best_arm,
            "best_jct": best_jct,
            "score_jct": score_mean,
            "pct_vs_score": pct,
            "history": history,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Hierarchical D-LinUCB + DSAC trainer")
    p.add_argument("--trace-family", default="philly")
    p.add_argument("--n-jobs", type=int, default=200)
    p.add_argument("--n-nodes", type=int, default=2)
    p.add_argument("--gpus-per-node", type=int, default=2)
    p.add_argument("--n-outer", type=int, default=5)
    p.add_argument("--n-inner", type=int, default=2000)
    p.add_argument("--utd-ratio", type=int, default=4)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--offline-buffer", type=int, default=5000)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out-dir", default=f"runs/hierarchical_{time.strftime('%Y%m%d-%H%M%S')}")
    args = p.parse_args(argv)

    trainer = HierarchicalTrainer(
        trace_family=args.trace_family,
        n_jobs=args.n_jobs,
        n_nodes=args.n_nodes,
        gpus_per_node=args.gpus_per_node,
        n_outer=args.n_outer,
        n_inner=args.n_inner,
        utd_ratio=args.utd_ratio,
        batch_size=args.batch_size,
        offline_buffer_size=args.offline_buffer,
        seed=args.seed,
        out_dir=args.out_dir,
    )
    result = trainer.train()
    print(f"\nDone. Best arm: {result['best_arm']}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
