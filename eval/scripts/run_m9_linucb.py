"""M9 evaluation — bandit-based weight tuning on the simulator.

Compares three policies on a (trace × seed) pool:

  random   uniform-arm baseline (worst case)
  ucb1     non-contextual UCB1
  linucb   contextual LinUCB

Each "round" the policy picks an arm (= score weight tuple
(alpha, delta, epsilon); beta fixed at 0.20) and the simulator returns
-mean_JCT in hours.

Outputs:
  eval/results/m9/m9_history.csv      per-round records (policy, round,
                                       arm, ctx, reward)
  eval/results/m9/m9_summary.json     final best arm per policy, total
                                       reward, comparison vs M8 grid
  eval/figures/fig9_m9_regret.{png,pdf}
                                      cumulative regret curves

Why this matters for the thesis:
  M8's E6 sensitivity grid (5×5 fixed cells per trace, 3 traces × 5 seeds
  = 375 simulator runs) defined the upper bound on how much weight
  tuning could help: philly 10.6%, burst 28.5%, ali 0.1%. M9 asks
  whether a learning policy can find the best cell *without* paying the
  full grid cost — i.e. is bandit weight tuning more sample-efficient
  than exhaustive search?
"""
from __future__ import annotations

import csv
import json
import os
import sys
from typing import List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "services")))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from weight_tuner.bandit import (  # noqa: E402
    LinUCBPolicy, RandomPolicy, UCB1Policy, train,
)
from weight_tuner.sim_env import (  # noqa: E402
    SimPull, TraceSpec, default_arm_grid,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_DIR = os.path.join(ROOT, "eval", "results", "m9")
FIG_DIR = os.path.join(ROOT, "eval", "figures")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(FIG_DIR, exist_ok=True)


# Trace pool — same as M8 evaluation, all 3 families × 5 seeds.
TRACES = [
    TraceSpec(family=fam, seed=seed, n_jobs=1000)
    for fam in ("philly", "burst", "ali")
    for seed in (42, 43, 44, 45, 46)
]
N_ROUNDS = 120                # << 27 arms × 15 contexts (=405), so bandit
                              # must generalise / explore efficiently
EVAL_ROUNDS = 30              # post-training pure-exploit eval


def build_pull() -> SimPull:
    pull = SimPull()
    for spec in TRACES:
        pull.register_trace(spec)
    return pull


def run_policy(name: str, pull: SimPull, contexts, seed: int):
    arms = default_arm_grid()
    if name == "random":
        policy = RandomPolicy(arms, seed=seed)
    elif name == "ucb1":
        policy = UCB1Policy(arms, c=0.4)
    elif name == "linucb":
        policy = LinUCBPolicy(arms, d=3, alpha=0.6, ridge=1.0)
    else:
        raise ValueError(name)
    result = train(policy, pull, contexts=contexts, n_rounds=N_ROUNDS, rng_seed=seed)
    return name, policy, result


def oracle_reward_per_context(pull: SimPull, contexts) -> dict:
    """For each context, return the best (=max-reward) arm and its reward.
    Used to compute regret against the omniscient optimum."""
    arms = default_arm_grid()
    best = {}
    for ctx in contexts:
        scores = [(a, pull(a, ctx)) for a in arms]
        a_star, r_star = max(scores, key=lambda p: p[1])
        best[tuple(ctx)] = (a_star, r_star)
    return best


def main() -> int:
    pull = build_pull()
    contexts = [pull._lookup[k] is not None and k for k in pull._lookup.keys()]
    # ^ ctx keys are just the registered context tuples
    contexts = list(pull._lookup.keys())
    print(f"[m9] {len(contexts)} contexts, {len(default_arm_grid())} arms, "
          f"{N_ROUNDS} train rounds + {EVAL_ROUNDS} eval rounds")

    # Pre-compute oracle (needed for regret + sanity).
    print("[m9] computing per-context oracle (this populates the sim cache)")
    oracle = oracle_reward_per_context(pull, contexts)

    runs = {}
    for name in ("random", "ucb1", "linucb"):
        print(f"[m9] training policy={name}")
        _, policy, result = run_policy(name, pull, contexts, seed=42)
        runs[name] = (policy, result)

    # Write per-round history.
    hist_csv = os.path.join(OUT_DIR, "m9_history.csv")
    with open(hist_csv, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["policy", "round", "alpha", "delta", "epsilon",
                    "ctx_n", "ctx_mps", "ctx_gpu", "reward"])
        for name, (_, result) in runs.items():
            for i, obs in enumerate(result.history):
                w.writerow([name, i, *obs.arm, *obs.context, obs.reward])
    print(f"[m9]   wrote {hist_csv}")

    # Eval phase: each policy fixed (greedy) on every context, average reward.
    eval_scores = {}
    for name, (policy, _) in runs.items():
        rewards = []
        for ctx in contexts:
            if name == "linucb":
                # Use mean prediction (no exploration bonus).
                arms = default_arm_grid()
                best = max(arms, key=lambda a: policy.predict(a, ctx))
            elif name == "ucb1":
                best = policy.best_arm()
            else:  # random — average over EVAL_ROUNDS random pulls
                import random
                rng = random.Random(99)
                rs = [pull(rng.choice(default_arm_grid()), ctx) for _ in range(EVAL_ROUNDS)]
                rewards.append(sum(rs) / len(rs))
                continue
            rewards.append(pull(best, ctx))
        eval_scores[name] = sum(rewards) / len(rewards)

    # Oracle = mean of best reward per context.
    oracle_score = sum(r for _, r in oracle.values()) / len(oracle)

    # M8 grid best (canonical fixed weight) — pick the arm with the highest
    # mean-reward across all contexts. This is what a static deployment
    # would use.
    arms = default_arm_grid()
    arm_means = {a: sum(pull(a, c) for c in contexts) / len(contexts) for a in arms}
    m8_best_arm = max(arm_means, key=arm_means.get)
    m8_best_score = arm_means[m8_best_arm]

    summary = {
        "n_rounds": N_ROUNDS,
        "n_contexts": len(contexts),
        "n_arms": len(arms),
        "eval_reward_random_h": -eval_scores["random"],
        "eval_reward_ucb1_h": -eval_scores["ucb1"],
        "eval_reward_linucb_h": -eval_scores["linucb"],
        "oracle_reward_h": -oracle_score,
        "m8_grid_best_reward_h": -m8_best_score,
        "m8_grid_best_arm": list(m8_best_arm),
        "ucb1_pulls_top5": sorted(
            ((list(a), n) for a, n in runs["ucb1"][0].pulls().items()),
            key=lambda p: -p[1],
        )[:5],
    }
    summary_path = os.path.join(OUT_DIR, "m9_summary.json")
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    print(f"[m9]   wrote {summary_path}")
    print()
    print("=== mean JCT (h) after training, evaluated greedily on every context ===")
    print(f"  random:  {-eval_scores['random']:.3f}")
    print(f"  ucb1:    {-eval_scores['ucb1']:.3f}")
    print(f"  linucb:  {-eval_scores['linucb']:.3f}")
    print(f"  M8 grid-best (static):  {-m8_best_score:.3f}   (arm={m8_best_arm})")
    print(f"  oracle (per-context):   {-oracle_score:.3f}")

    # Fig: cumulative regret over training rounds.
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for name, color in (("random", "C3"), ("ucb1", "C0"), ("linucb", "C2")):
        result = runs[name][1]
        rewards_oracle = [oracle[obs.context][1] for obs in result.history]
        regret = np.cumsum([oracle - obs.reward for obs, oracle in zip(result.history, rewards_oracle)])
        ax.plot(regret, label=name, color=color, linewidth=1.6)
    ax.set_xlabel("training round")
    ax.set_ylabel("cumulative regret\n(oracle_h − observed_h, summed)")
    ax.set_title(f"M9 LinUCB vs UCB1 vs random — {N_ROUNDS} rounds, "
                 f"{len(arms)} arms, {len(contexts)} contexts")
    ax.grid(linestyle=":", alpha=0.6)
    ax.legend()
    out = os.path.join(FIG_DIR, "fig9_m9_regret")
    for ext in ("png", "pdf"):
        fig.savefig(f"{out}.{ext}", dpi=160, bbox_inches="tight")
    plt.close(fig)
    print(f"[m9]   wrote {out}.{{png,pdf}}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
