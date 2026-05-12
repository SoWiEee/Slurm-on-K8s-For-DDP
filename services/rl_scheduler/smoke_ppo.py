"""M11 Phase A SB3 PPO smoke test: 1000 steps on a 30-job synthetic trace.

Verifies:
- gym env is SB3-compatible (Box obs + Discrete action)
- PPO runs end-to-end without NaN
- GPU is utilized if CUDA available

Run: .venv-m11/bin/python -m services.rl_scheduler.smoke_ppo
"""
from __future__ import annotations

import argparse
import sys
import time

import torch
from stable_baselines3 import PPO
from stable_baselines3.common.env_checker import check_env

from sim.gym_env import KubefluxSchedGymEnv
from sim.loader import generate_by_family


def make_env(n_jobs: int = 30, seed: int = 42):
    def _factory():
        return generate_by_family("philly", n_jobs=n_jobs, seed=seed)
    return KubefluxSchedGymEnv(
        jobs_factory=_factory,
        n_nodes=4,
        gpus_per_node=4,
        max_steps=10_000,
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--total-steps", type=int, default=1000)
    p.add_argument("--skip-check", action="store_true")
    args = p.parse_args(argv)

    print(f"torch={torch.__version__}, cuda_available={torch.cuda.is_available()}")
    if torch.cuda.is_available():
        print(f"  device={torch.cuda.get_device_name(0)}")
    device = "cuda" if torch.cuda.is_available() else "cpu"

    env = make_env()
    if not args.skip_check:
        check_env(env, warn=True)
        print("env_checker: OK")

    model = PPO(
        "MlpPolicy",
        env,
        device=device,
        n_steps=128,
        batch_size=64,
        n_epochs=4,
        verbose=1,
        policy_kwargs={"net_arch": [256, 128]},
    )
    t0 = time.monotonic()
    model.learn(total_timesteps=args.total_steps, progress_bar=False)
    elapsed = time.monotonic() - t0
    print(f"\nPPO smoke OK: {args.total_steps} steps in {elapsed:.1f}s "
          f"({args.total_steps/elapsed:.0f} steps/s) on {device}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
