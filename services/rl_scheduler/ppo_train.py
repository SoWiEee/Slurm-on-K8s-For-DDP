"""M11 Phase B: PPO training pipeline for sim-trained scheduler.

Wraps ``sim.gym_env.KubefluxSchedGymEnv`` in a SubprocVecEnv + VecNormalize,
runs PPO with TensorBoard logging and periodic evaluation against the
``score`` baseline scheduler on a held-out seed.

Run:
    .venv-m11/bin/python -m services.rl_scheduler.ppo_train \\
        --total-steps 100000 --n-envs 4 --n-jobs 200

Output:
    runs/m11_ppo_<timestamp>/
        policy.zip            -- final SB3 model
        vecnormalize.pkl      -- saved obs/reward normaliser
        eval_log.csv          -- per-checkpoint eval JCT vs score baseline
        tb/                   -- TensorBoard event files
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from pathlib import Path

import numpy as np
import torch
from stable_baselines3 import PPO
from stable_baselines3.common.callbacks import BaseCallback, CheckpointCallback
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.vec_env import DummyVecEnv, SubprocVecEnv, VecNormalize

from sim.gym_env import KubefluxSchedGymEnv
from sim.loader import generate_by_family


# ---------------------------------------------------------------------------
def make_env_fn(*, trace_family: str, n_jobs: int, seed: int,
                n_nodes: int, gpus_per_node: int, max_steps: int):
    """Return a thunk that constructs a fresh env (required by SubprocVecEnv)."""
    def _thunk():
        rng = np.random.default_rng(seed)
        def _factory():
            # Different episode seeds so PPO sees varied traces
            ep_seed = int(rng.integers(0, 2**31 - 1))
            return generate_by_family(trace_family, n_jobs=n_jobs, seed=ep_seed)
        env = KubefluxSchedGymEnv(
            jobs_factory=_factory,
            n_nodes=n_nodes,
            gpus_per_node=gpus_per_node,
            max_steps=max_steps,
        )
        return Monitor(env)
    return _thunk


# ---------------------------------------------------------------------------
def eval_baseline_score(*, trace_family: str, n_jobs: int, seed: int,
                        n_nodes: int, gpus_per_node: int) -> float:
    """Run sim.runner with the score scheduler on a deterministic seed,
    return avg JCT (lower is better)."""
    from sim.runner import run
    jobs = generate_by_family(trace_family, n_jobs=n_jobs, seed=seed)
    metrics, _ = run(
        jobs,
        n_nodes=n_nodes,
        gpus_per_node=gpus_per_node,
        scheduler_name="score",
    )
    return metrics.summary()["jct_mean"]


def eval_policy_jct(model, env_thunk, n_episodes: int = 3) -> float:
    """Roll out trained policy on env_thunk for n_episodes, average avg_jct."""
    jcts = []
    for _ in range(n_episodes):
        env = env_thunk()
        obs, _ = env.reset()
        terminated = truncated = False
        info = {}
        while not (terminated or truncated):
            action, _ = model.predict(obs, deterministic=True)
            obs, _r, terminated, truncated, info = env.step(int(action))
        if "avg_jct" in info:
            jcts.append(info["avg_jct"])
        env.close()
    return float(np.mean(jcts)) if jcts else float("nan")


# ---------------------------------------------------------------------------
class EvalAgainstScoreCallback(BaseCallback):
    """Every ``eval_freq`` rollout updates, evaluate PPO vs score baseline."""
    def __init__(self, *, eval_freq: int, eval_kwargs: dict, log_path: str,
                 verbose: int = 0):
        super().__init__(verbose)
        self.eval_freq = eval_freq
        self.eval_kwargs = eval_kwargs
        self.log_path = log_path
        self._last_eval_step = 0
        # Pre-compute score baseline (deterministic per seed; one number)
        self._score_jct = eval_baseline_score(**eval_kwargs)
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        with open(self.log_path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["step", "ppo_avg_jct", "score_avg_jct", "ratio"])

    def _on_step(self) -> bool:
        if self.num_timesteps - self._last_eval_step < self.eval_freq:
            return True
        self._last_eval_step = self.num_timesteps

        ek = self.eval_kwargs
        eval_thunk = lambda: KubefluxSchedGymEnv(
            jobs_factory=lambda: generate_by_family(
                ek["trace_family"], n_jobs=ek["n_jobs"], seed=ek["seed"]),
            n_nodes=ek["n_nodes"],
            gpus_per_node=ek["gpus_per_node"],
            max_steps=ek["n_jobs"] * 100,
        )
        ppo_jct = eval_policy_jct(self.model, eval_thunk, n_episodes=1)
        ratio = ppo_jct / self._score_jct if self._score_jct > 0 else float("nan")
        if self.verbose:
            print(f"  [eval@{self.num_timesteps}] ppo_jct={ppo_jct:.1f}  "
                  f"score_jct={self._score_jct:.1f}  ratio={ratio:.3f}")
        with open(self.log_path, "a", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([self.num_timesteps, ppo_jct, self._score_jct, ratio])
        # Log to TB
        self.logger.record("eval/ppo_avg_jct", ppo_jct)
        self.logger.record("eval/score_avg_jct", self._score_jct)
        self.logger.record("eval/ratio_ppo_over_score", ratio)
        return True


# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--total-steps", type=int, default=100_000)
    p.add_argument("--n-envs", type=int, default=4)
    p.add_argument("--n-jobs", type=int, default=200,
                   help="jobs per episode (training trace length)")
    p.add_argument("--n-nodes", type=int, default=4)
    p.add_argument("--gpus-per-node", type=int, default=4)
    p.add_argument("--trace-family", default="philly",
                   choices=["philly", "burst", "ali"])
    p.add_argument("--seed-base", type=int, default=42)
    p.add_argument("--eval-seed", type=int, default=999,
                   help="held-out seed for vs-score eval (different from training)")
    p.add_argument("--eval-freq", type=int, default=10_000)
    p.add_argument("--out-dir", default=None,
                   help="default: runs/m11_ppo_<timestamp>")
    p.add_argument("--no-subproc", action="store_true",
                   help="use DummyVecEnv instead of SubprocVecEnv (debug)")
    args = p.parse_args(argv)

    ts = time.strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir or f"runs/m11_ppo_{ts}")
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"output dir: {out_dir}")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"torch={torch.__version__} device={device}")

    max_steps = args.n_jobs * 100  # generous truncation
    env_fns = [
        make_env_fn(trace_family=args.trace_family,
                    n_jobs=args.n_jobs,
                    seed=args.seed_base + i,
                    n_nodes=args.n_nodes,
                    gpus_per_node=args.gpus_per_node,
                    max_steps=max_steps)
        for i in range(args.n_envs)
    ]
    vec_cls = DummyVecEnv if args.no_subproc else SubprocVecEnv
    vec_env = vec_cls(env_fns)
    vec_env = VecNormalize(vec_env, norm_obs=True, norm_reward=True,
                           clip_obs=10.0, clip_reward=10.0)

    model = PPO(
        "MlpPolicy",
        vec_env,
        device=device,
        n_steps=256,
        batch_size=128,
        n_epochs=4,
        learning_rate=3e-4,
        ent_coef=0.01,
        gamma=0.99,
        gae_lambda=0.95,
        clip_range=0.2,
        verbose=1,
        tensorboard_log=str(out_dir / "tb"),
        policy_kwargs={"net_arch": [256, 128]},
    )

    eval_cb = EvalAgainstScoreCallback(
        eval_freq=args.eval_freq,
        eval_kwargs={
            "trace_family": args.trace_family,
            "n_jobs": args.n_jobs,
            "seed": args.eval_seed,
            "n_nodes": args.n_nodes,
            "gpus_per_node": args.gpus_per_node,
        },
        log_path=str(out_dir / "eval_log.csv"),
        verbose=1,
    )
    ckpt_cb = CheckpointCallback(
        save_freq=max(1, args.eval_freq // args.n_envs),
        save_path=str(out_dir / "checkpoints"),
        name_prefix="ppo",
    )

    t0 = time.monotonic()
    model.learn(total_timesteps=args.total_steps, callback=[eval_cb, ckpt_cb])
    elapsed = time.monotonic() - t0

    model.save(str(out_dir / "policy"))
    vec_env.save(str(out_dir / "vecnormalize.pkl"))
    print(f"\ntraining done: {args.total_steps} steps in {elapsed:.1f}s "
          f"({args.total_steps/elapsed:.0f} steps/s)")
    print(f"final policy: {out_dir/'policy.zip'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
