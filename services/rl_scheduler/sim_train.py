"""Step 4: Online DSAC training loop inside KubefluxSchedEnv.

Runs the DSAC policy online in sim: agent collects its own transitions,
updates at UTD=4 after every env step (once the warmup buffer is full).
Saves checkpoint + JSONL episode log.

Improvements:
  n-step returns      : pre-compute discounted return over n steps (n=10 default)
  score warmup        : use score scheduler during warmup for high-quality seeds
  short episodes      : default n_jobs=50 → ~3× more distinct episodes
  potential shaping   : per-step reward φ(s) = −Σwait/scale, Ng et al. 1999
  PER                 : prioritized replay, sample by TD-error magnitude
  CQL                 : conservative Q-Learning penalty, reduces overestimation
  IQN                 : Implicit Quantile Network critic (opt-in)
  Curriculum          : n_jobs ramps from easy to hard over training

Usage::
    .venv-m11/bin/python -m services.rl_scheduler.sim_train \\
        --n-nodes 1 --gpus-per-node 1 --total-steps 500000 \\
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
from services.rl_scheduler.rlpd_finetune import (
    PrioritizedReplayBuffer, ReplayBuffer, Transition,
)


def _flush_nstep(nstep_buf: deque, buf, gamma: float) -> None:
    """Commit the oldest transition in nstep_buf with its n-step return."""
    if not nstep_buf:
        return
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
    t_last = nstep_buf[horizon - 1]
    buf.add(
        Transition(
            obs=t0.obs, act=t0.act, rew=nstep_rew,
            next_obs=t_last.next_obs, done=t_last.done,
            mask=t0.mask, next_mask=t_last.next_mask,
        ),
        gamma=g,
    )


def sim_train(
    *,
    n_nodes: int = 1,
    gpus_per_node: int = 1,
    trace_family: str | list = "philly",
    n_jobs: int = 50,
    nstep_n: int = 10,
    total_steps: int = 500_000,
    warmup_steps: int = 2_000,
    score_warmup: bool = True,
    update_every: int = 1,
    utd_ratio: int = 4,
    batch_size: int = 256,
    buf_capacity: int = 100_000,
    seed: int = 42,
    out_dir: Optional[Path] = None,
    reward_mode: str = "jct_aligned",
    device: str = "cpu",
    log_every: int = 5_000,
    use_attention: bool = False,
    # New improvements
    potential_shaping: bool = True,
    use_per: bool = True,
    use_iqn: bool = False,
    cql_alpha: float = 0.1,
    curriculum: bool = False,
    curriculum_stages: Optional[list] = None,
) -> DSACAgent:
    """Run online DSAC training in sim. Returns the trained agent."""
    obs_dim, n_actions = env_dims(n_nodes, gpus_per_node)
    rng = np.random.default_rng(seed)

    total_gpus = n_nodes * gpus_per_node
    families = [trace_family] if isinstance(trace_family, str) else list(trace_family)

    # Curriculum stages: list of (n_jobs, fraction_of_total_steps)
    if curriculum and curriculum_stages is None:
        curriculum_stages = [(10, 0.2), (30, 0.3), (50, 0.5)]

    active_n_jobs = n_jobs

    def _make_factory(nj: int):
        def _factory():
            family = families[int(rng.integers(0, len(families)))]
            jobs = generate_by_family(family, n_jobs=nj,
                                      seed=int(rng.integers(0, 2**31 - 1)))
            return [j for j in jobs if j.gpu_count <= total_gpus]
        return _factory

    env = KubefluxSchedEnv(
        _make_factory(active_n_jobs),
        n_nodes=n_nodes, gpus_per_node=gpus_per_node,
        max_steps=active_n_jobs * 200,
        reward_mode=reward_mode,
        potential_shaping=potential_shaping,
    )
    agent = DSACAgent(
        obs_dim=obs_dim, n_actions=n_actions, device=device,
        use_attention=use_attention, use_iqn=use_iqn, cql_alpha=cql_alpha,
    )

    if use_per:
        buf = PrioritizedReplayBuffer(
            capacity=buf_capacity, obs_dim=obs_dim, n_actions=n_actions,
        )
    else:
        buf = ReplayBuffer(capacity=buf_capacity, obs_dim=obs_dim, n_actions=n_actions)

    score_sched = None
    if score_warmup:
        from sim.scheduler.score import ScoreScheduler
        score_sched = ScoreScheduler()

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
        # ── Curriculum: switch n_jobs when crossing a stage boundary ────
        if curriculum_stages is not None:
            progress = step / total_steps
            cum = 0.0
            stage_n_jobs = curriculum_stages[0][0]
            for nj, frac in curriculum_stages:
                cum += frac
                if progress < cum:
                    stage_n_jobs = nj
                    break
            if stage_n_jobs != active_n_jobs:
                active_n_jobs = stage_n_jobs
                env.jobs_factory = _make_factory(active_n_jobs)
                env.max_steps    = active_n_jobs * 200
                print(f"  [curriculum] step={step}: n_jobs → {active_n_jobs}")

        mask = env.action_mask()

        if len(buf) < warmup_steps:
            state = env._state
            if (score_sched is not None and state is not None
                    and state.pending and state.cluster):
                ordered = score_sched.order(state.pending, state.cluster,
                                            now=state.now)
                legal = np.flatnonzero(mask)
                act = int(rng.choice(legal))
                for job in ordered:
                    job_idx = next(
                        (i for i, j in enumerate(state.pending)
                         if j.job_id == job.job_id),
                        None,
                    )
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
                    "n_jobs": active_n_jobs,
                }) + "\n")
            ep_steps = ep_reward = 0.0
            obs, _ = env.reset()

        # ── Gradient updates — only after warmup ────────────────────────
        if len(buf) >= warmup_steps and step % update_every == 0:
            for _ in range(utd_ratio):
                batch = buf.sample(min(batch_size, len(buf)), rng)
                losses = agent.update(batch)
                # PER: update priorities with new TD errors
                if use_per and "indices" in batch and "td_errors" in losses:
                    buf.update_priorities(batch["indices"], losses["td_errors"])

        if (step + 1) % log_every == 0:
            elapsed = time.time() - t0
            print(f"  step {step+1:6d}/{total_steps}  buf={len(buf):6d}  "
                  f"eps={ep_count}  n_jobs={active_n_jobs}  "
                  f"alpha={agent.alpha.item():.3f}  elapsed={elapsed:.0f}s")

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
                   nargs="+", choices=["philly", "burst", "ali"])
    p.add_argument("--n-jobs",        type=int, default=50)
    p.add_argument("--nstep-n",       type=int, default=10)
    p.add_argument("--no-score-warmup", action="store_true")
    p.add_argument("--total-steps",   type=int, default=500_000)
    p.add_argument("--warmup-steps",  type=int, default=2_000)
    p.add_argument("--utd-ratio",     type=int, default=4)
    p.add_argument("--batch-size",    type=int, default=256)
    p.add_argument("--seed",          type=int, default=42)
    p.add_argument("--reward-mode",   default="jct_aligned",
                   choices=["jct_aligned", "shaped"])
    p.add_argument("--device",        default="cpu")
    p.add_argument("--out-dir",
                   default=f"runs/dsac_sim_{time.strftime('%Y%m%d-%H%M%S')}")
    # Architecture flags
    p.add_argument("--no-attention",         action="store_true")
    p.add_argument("--use-iqn",              action="store_true",
                   help="IQN critic (quantile Huber loss)")
    # Improvement flags
    p.add_argument("--no-potential-shaping", action="store_true",
                   help="disable potential-based reward shaping")
    p.add_argument("--no-per",               action="store_true",
                   help="disable Prioritized Experience Replay")
    p.add_argument("--cql-alpha",            type=float, default=0.1,
                   help="CQL penalty weight (0 = disabled)")
    p.add_argument("--curriculum",           action="store_true",
                   help="ramp n_jobs: 10→30→50 over training")
    args = p.parse_args(argv)

    import torch
    device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        print("[sim_train] CUDA not available, falling back to CPU")
        device = "cpu"

    traces = args.trace if len(args.trace) > 1 else args.trace[0]
    use_attention = not args.no_attention
    arch = "IQN" if args.use_iqn else ("Attention" if use_attention else "MLP")
    print(f"[sim_train] arch={arch}  n={args.n_nodes}×{args.gpus_per_node}  "
          f"trace={traces}  steps={args.total_steps:,}  "
          f"n_jobs={args.n_jobs}  nstep={args.nstep_n}  "
          f"PER={not args.no_per}  shaping={not args.no_potential_shaping}  "
          f"CQL={args.cql_alpha}  curriculum={args.curriculum}  device={device}")
    sim_train(
        n_nodes=args.n_nodes, gpus_per_node=args.gpus_per_node,
        trace_family=traces, n_jobs=args.n_jobs,
        nstep_n=args.nstep_n, score_warmup=not args.no_score_warmup,
        total_steps=args.total_steps, warmup_steps=args.warmup_steps,
        utd_ratio=args.utd_ratio, batch_size=args.batch_size,
        seed=args.seed, reward_mode=args.reward_mode,
        out_dir=Path(args.out_dir), device=device,
        use_attention=use_attention,
        potential_shaping=not args.no_potential_shaping,
        use_per=not args.no_per,
        use_iqn=args.use_iqn,
        cql_alpha=args.cql_alpha,
        curriculum=args.curriculum,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
