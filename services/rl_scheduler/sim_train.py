"""Step 4: Online DSAC training loop inside KubefluxSchedEnv.

Runs the DSAC policy online in sim: agent collects its own transitions,
updates at UTD=4 after every env step (once the warmup buffer is full).
Saves checkpoint + JSONL episode log.

Improvements over baseline:
  n-step returns  : pre-compute discounted return over n steps before storing
                    in the replay buffer → reduces credit-assignment lag for
                    sparse JCT reward (n=10 default).
  score warmup    : during warmup use the score scheduler (not uniform random)
                    → higher-quality seed transitions in the buffer.
  short episodes  : default n_jobs=50 → ~800 steps/episode → ~3× more
                    distinct episodes in the same step budget.

Usage::
    .venv-m11/bin/python -m services.rl_scheduler.sim_train \\
        --n-nodes 1 --gpus-per-node 1 --total-steps 50000 \\
        --trace philly --n-jobs 50 --out-dir runs/dsac_sim
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np

from sim.gym_env import KubefluxSchedEnv, env_dims
from sim.loader import generate_by_family
from services.rl_scheduler.dsac import DSACAgent
from services.rl_scheduler.rlpd_finetune import ReplayBuffer, Transition


def _flush_nstep(nstep_buf: deque, buf: ReplayBuffer, gamma: float) -> None:
    """Commit the oldest transition in nstep_buf with its n-step return.

    Accumulates r_t + γ·r_{t+1} + ... + γ^{n-1}·r_{t+n-1} as the stored
    reward, and γ^n as the effective discount for the bootstrapped value.
    If any transition in the window is terminal the n-step horizon is
    truncated there (standard n-step return with done masking).
    """
    if not nstep_buf:
        return
    # Find first done within the window → truncate horizon there
    horizon = len(nstep_buf)
    for h, t in enumerate(nstep_buf):
        if t.done:
            horizon = h + 1
            break

    t0 = nstep_buf[0]
    nstep_rew = 0.0
    g = 1.0
    for h in range(horizon):
        nstep_rew += g * nstep_buf[h].rew
        g *= gamma
    # next_obs and next_mask come from the last step in the horizon
    t_last = nstep_buf[horizon - 1]
    buf.add(
        Transition(
            obs=t0.obs, act=t0.act, rew=nstep_rew,
            next_obs=t_last.next_obs, done=t_last.done,
            mask=t0.mask, next_mask=t_last.next_mask,
        ),
        gamma=g,   # γ^n (or γ^k if truncated by done)
    )


def sim_train(
    *,
    n_nodes: int = 1,
    gpus_per_node: int = 1,
    trace_family: str | list = "philly",
    n_jobs: int = 50,          # shorter episodes → more distinct scenarios
    nstep_n: int = 10,         # n-step return horizon
    total_steps: int = 50_000,
    warmup_steps: int = 2_000,
    score_warmup: bool = True,  # use score scheduler during warmup
    update_every: int = 1,
    utd_ratio: int = 4,
    batch_size: int = 256,
    buf_capacity: int = 100_000,
    seed: int = 42,
    out_dir: Optional[Path] = None,
    reward_mode: str = "jct_aligned",
    device: str = "cpu",
    log_every: int = 5_000,
) -> DSACAgent:
    """Run online DSAC training in sim. Returns the trained agent."""
    obs_dim, n_actions = env_dims(n_nodes, gpus_per_node)
    rng = np.random.default_rng(seed)

    total_gpus = n_nodes * gpus_per_node
    families = [trace_family] if isinstance(trace_family, str) else list(trace_family)

    def _factory():
        family = families[int(rng.integers(0, len(families)))]
        jobs = generate_by_family(family, n_jobs=n_jobs,
                                   seed=int(rng.integers(0, 2**31 - 1)))
        return [j for j in jobs if j.gpu_count <= total_gpus]

    env = KubefluxSchedEnv(
        _factory,
        n_nodes=n_nodes, gpus_per_node=gpus_per_node,
        max_steps=n_jobs * 200,
        reward_mode=reward_mode,
    )
    agent = DSACAgent(obs_dim=obs_dim, n_actions=n_actions, device=device)
    buf   = ReplayBuffer(capacity=buf_capacity, obs_dim=obs_dim, n_actions=n_actions)

    # Score-guided warmup: use score scheduler for high-quality seed data
    score_sched = None
    if score_warmup:
        from sim.scheduler.score import ScoreScheduler
        score_sched = ScoreScheduler()

    # n-step return sliding window
    nstep_buf: deque = deque(maxlen=nstep_n)

    log_fh = None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        log_fh = open(out_dir / "sim_train.jsonl", "w")

    obs, _ = env.reset(seed=seed)
    ep_steps = ep_reward = 0.0
    ep_count = 0
    t0 = time.time()

    for step in range(total_steps):
        mask = env.action_mask()

        if len(buf) < warmup_steps:
            # Score-guided warmup: score scheduler picks action from legal set.
            # _state.pending and _state.cluster expose the current env state.
            state = env._state
            if (score_sched is not None and state is not None
                    and state.pending and state.cluster):
                ordered = score_sched.order(state.pending, state.cluster,
                                            now=state.now)
                legal = np.flatnonzero(mask)
                act = int(rng.choice(legal))  # fallback
                for job in ordered:
                    job_idx = next(
                        (i for i, j in enumerate(state.pending)
                         if j.job_id == job.job_id),
                        None,
                    )
                    # action index = job_idx * n_placements + gpu_slot (0 for 1×1)
                    if job_idx is not None:
                        candidate = job_idx * env._n_placements
                        if candidate < len(mask) and mask[candidate]:
                            act = int(candidate)
                            break
            else:
                act = int(rng.choice(np.flatnonzero(mask)))
        else:
            act = agent.select_action(obs, mask)

        next_obs, rew, term, trunc, info = env.step(act)
        next_mask = env.action_mask()

        done = bool(term or trunc)
        nstep_buf.append(Transition(
            obs=obs, act=act, rew=float(rew),
            next_obs=next_obs, done=done,
            mask=mask, next_mask=next_mask,
        ))

        # Commit to replay buffer when window is full or episode ends
        if len(nstep_buf) == nstep_n or done:
            _flush_nstep(nstep_buf, buf, gamma=agent.gamma)
            if done:
                nstep_buf.clear()

        obs = next_obs
        ep_steps += 1
        ep_reward += float(rew)

        if done:
            ep_count += 1
            if log_fh:
                log_fh.write(json.dumps({
                    "step": step, "episode": ep_count,
                    "ep_steps": int(ep_steps), "ep_reward": ep_reward,
                    "avg_jct": info.get("avg_jct", float("nan")),
                    "completed": info.get("completed", 0),
                }) + "\n")
            ep_steps = ep_reward = 0.0
            obs, _ = env.reset()

        # Gradient updates — only after warmup
        if len(buf) >= warmup_steps and step % update_every == 0:
            for _ in range(utd_ratio):
                batch = buf.sample(min(batch_size, len(buf)), rng)
                agent.update(batch)

        if (step + 1) % log_every == 0:
            elapsed = time.time() - t0
            print(f"  step {step+1:6d}/{total_steps}  buf={len(buf):6d}  "
                  f"eps={ep_count}  alpha={agent.alpha.item():.3f}  "
                  f"elapsed={elapsed:.0f}s")

    env.close()
    if log_fh:
        log_fh.close()

    if out_dir:
        ckpt = out_dir / "dsac.pt"
        agent.save(ckpt)
        print(f"[sim_train] saved → {ckpt}")

    return agent


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--n-nodes",       type=int, default=1)
    p.add_argument("--gpus-per-node", type=int, default=1)
    p.add_argument("--trace",         default=["philly", "burst", "ali"],
                   nargs="+", choices=["philly", "burst", "ali"],
                   help="trace family/families; multiple = mixed training")
    p.add_argument("--n-jobs",        type=int, default=50,
                   help="jobs per episode (shorter = more episodes; default 50)")
    p.add_argument("--nstep-n",       type=int, default=10,
                   help="n-step return horizon (1 = standard 1-step TD)")
    p.add_argument("--no-score-warmup", action="store_true",
                   help="disable score-guided warmup (use uniform random instead)")
    p.add_argument("--total-steps",   type=int, default=50_000)
    p.add_argument("--warmup-steps",  type=int, default=2_000)
    p.add_argument("--utd-ratio",     type=int, default=4)
    p.add_argument("--batch-size",    type=int, default=256)
    p.add_argument("--seed",          type=int, default=42)
    p.add_argument("--reward-mode",   default="jct_aligned",
                   choices=["jct_aligned", "shaped"])
    p.add_argument("--device",        default="cpu",
                   help="torch device: 'cpu' or 'cuda'")
    p.add_argument("--out-dir",
                   default=f"runs/dsac_sim_{time.strftime('%Y%m%d-%H%M%S')}")
    args = p.parse_args(argv)

    import torch
    device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        print("[sim_train] CUDA requested but not available, falling back to CPU")
        device = "cpu"
    traces = args.trace if len(args.trace) > 1 else args.trace[0]
    print(f"[sim_train] n={args.n_nodes}×{args.gpus_per_node}  "
          f"trace={traces}  steps={args.total_steps:,}  "
          f"n_jobs={args.n_jobs}  nstep={args.nstep_n}  "
          f"score_warmup={not args.no_score_warmup}  "
          f"UTD={args.utd_ratio}  device={device}")
    sim_train(
        n_nodes=args.n_nodes, gpus_per_node=args.gpus_per_node,
        trace_family=traces, n_jobs=args.n_jobs,
        nstep_n=args.nstep_n, score_warmup=not args.no_score_warmup,
        total_steps=args.total_steps, warmup_steps=args.warmup_steps,
        utd_ratio=args.utd_ratio, batch_size=args.batch_size,
        seed=args.seed, reward_mode=args.reward_mode,
        out_dir=Path(args.out_dir), device=device,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
