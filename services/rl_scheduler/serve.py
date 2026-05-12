"""M11 Phase C-2: FastAPI inference endpoint for the trained RL scheduler.

Loads ``policy.zip`` + ``vecnormalize.pkl`` from a runs/ directory and
exposes:

- GET  /healthz   — readiness (model loaded)
- POST /act       — given a structured cluster snapshot, return action +
                     selected job id + value/entropy diagnostics for the
                     Phase D safety-net wrapper

The request schema is intentionally structured rather than raw obs floats
so the Slurm lua plugin doesn't need numpy. Server-side we replicate
``sim/gym_env.py`` feature construction so train / serve don't drift.

Run:
    .venv-m11/bin/python -m services.rl_scheduler.serve \\
        --policy-dir runs/m11_mppo_20260512-155707 --port 8002
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path
from typing import List, Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from stable_baselines3 import PPO
from stable_baselines3.common.vec_env import DummyVecEnv, VecNormalize

try:
    from sb3_contrib import MaskablePPO
except ImportError:
    MaskablePPO = None  # type: ignore

from sim.gym_env import (
    GLOBAL_FEAT_DIM,
    GPU_TYPES,
    JOB_FEAT_DIM,
    KubefluxSchedGymEnv,
    MAX_NODES,
    NODE_FEAT_DIM,
    TOP_K,
)
from sim.loader import MPS_PER_GPU


# ---------- request / response schemas ----------
class JobView(BaseModel):
    job_id: str
    mps_req: int
    gpu_count: int
    gpu_type: str = "rtx4070"
    runtime: float          # predicted seconds; serve uses log1p
    submit_ts: float
    can_fit: bool = True    # caller-side feasibility (action_mask)


class NodeView(BaseModel):
    free_mps: int
    free_vram: int = 0      # default = free_mps (MPS == VRAM proxy in sim)
    running_jobs: int = 0


class ActRequest(BaseModel):
    now: float
    pending_jobs: List[JobView] = Field(default_factory=list)
    nodes: List[NodeView] = Field(default_factory=list)
    n_nodes: int = 1
    gpus_per_node: int = 1
    mps_per_gpu: int = MPS_PER_GPU


class ActResponse(BaseModel):
    action: int                 # raw discrete index; TOP_K == no-op
    selected_job_id: Optional[str]
    value: float                # critic estimate (for safety-net low-value abstain)
    entropy: float              # action-distribution entropy (for safety-net)
    top_k_used: int


# ---------- feature construction (mirrors sim/gym_env.py) ----------
def _job_feat(j: JobView, now: float, mps_per_gpu: int) -> np.ndarray:
    gpu_oh = [1.0 if j.gpu_type == t else 0.0 for t in GPU_TYPES]
    wait = max(0.0, now - j.submit_ts)
    return np.array([
        j.mps_req / mps_per_gpu,
        float(j.gpu_count),
        *gpu_oh,
        math.log1p(j.runtime),
        math.log1p(wait),
        math.log1p(wait),
        0.0, 0.0,
    ], dtype=np.float32)


def _node_feat(n: NodeView, total_mps: int) -> np.ndarray:
    return np.array([
        n.free_mps / total_mps if total_mps > 0 else 0.0,
        n.free_vram / total_mps if total_mps > 0 else 0.0,
        float(n.running_jobs),
    ], dtype=np.float32)


def _global_feat(req: ActRequest) -> np.ndarray:
    queue_len = len(req.pending_jobs)
    free_per_node = [n.free_mps for n in req.nodes] if req.nodes else [0]
    if len(free_per_node) > 1:
        mean = max(1.0, sum(free_per_node) / len(free_per_node))
        var = sum((x - mean) ** 2 for x in free_per_node) / len(free_per_node)
        frag = math.sqrt(var) / mean
    else:
        frag = 0.0
    tod = (req.now % 86400) / 86400.0
    return np.array([
        math.log1p(queue_len),
        1.0,
        frag,
        math.sin(2 * math.pi * tod),
        math.cos(2 * math.pi * tod),
    ], dtype=np.float32)


def build_obs(req: ActRequest) -> tuple[np.ndarray, np.ndarray, List[Optional[str]]]:
    """Return (obs, action_mask, top_k_job_ids).

    top_k_job_ids[i] is the job_id at action-index i (or None for padding).
    """
    pending_sorted = sorted(req.pending_jobs, key=lambda j: j.submit_ts)[:TOP_K]
    feats = []
    top_ids: List[Optional[str]] = []
    mask = np.zeros(TOP_K + 1, dtype=bool)
    for i in range(TOP_K):
        if i < len(pending_sorted):
            j = pending_sorted[i]
            feats.append(_job_feat(j, req.now, req.mps_per_gpu))
            top_ids.append(j.job_id)
            if j.can_fit:
                mask[i] = True
        else:
            feats.append(np.zeros(JOB_FEAT_DIM, dtype=np.float32))
            top_ids.append(None)
    total_mps = req.mps_per_gpu * req.gpus_per_node
    node_feats = []
    for ni in range(MAX_NODES):
        if ni < len(req.nodes):
            node_feats.append(_node_feat(req.nodes[ni], total_mps))
        else:
            node_feats.append(np.zeros(NODE_FEAT_DIM, dtype=np.float32))
    glob = _global_feat(req)
    mask[TOP_K] = True  # no-op always legal
    obs = np.concatenate([*feats, *node_feats, glob]).astype(np.float32)
    return obs, mask, top_ids


# ---------- model holder ----------
class ModelHolder:
    def __init__(self, policy_dir: Path):
        self.policy_dir = policy_dir
        self.masked = (policy_dir / "MASKED").exists()
        policy_path = policy_dir / "policy.zip"
        if not policy_path.exists():
            policy_path = policy_dir / "policy"
        if not policy_path.exists():
            raise FileNotFoundError(f"no policy.zip under {policy_dir}")
        vecnorm_path = policy_dir / "vecnormalize.pkl"
        if not vecnorm_path.exists():
            raise FileNotFoundError(f"no vecnormalize.pkl under {policy_dir}")

        if self.masked:
            if MaskablePPO is None:
                raise RuntimeError("MASKED policy but sb3-contrib not installed")
            self.model = MaskablePPO.load(str(policy_path), device="cpu")
        else:
            self.model = PPO.load(str(policy_path), device="cpu")
        # Wrap a dummy single-env just to host VecNormalize for obs normalisation.
        dummy = DummyVecEnv([self._dummy_env_thunk()])
        self.vecnorm = VecNormalize.load(str(vecnorm_path), dummy)
        self.vecnorm.training = False
        self.vecnorm.norm_reward = False
        print(f"[serve] loaded {'MaskablePPO' if self.masked else 'PPO'} "
              f"from {policy_dir}")

    def _dummy_env_thunk(self):
        def _factory():
            from sim.loader import generate_by_family
            return generate_by_family("philly", n_jobs=8, seed=0)
        def _thunk():
            return KubefluxSchedGymEnv(
                jobs_factory=_factory,
                n_nodes=1,
                gpus_per_node=1,
                max_steps=10,
            )
        return _thunk

    def predict(self, obs: np.ndarray, mask: np.ndarray):
        norm_obs = self.vecnorm.normalize_obs(obs[np.newaxis, :])
        kw = {"action_masks": mask[np.newaxis, :]} if self.masked else {}
        action, _ = self.model.predict(norm_obs, deterministic=True, **kw)
        # Diagnostics: value + entropy from the policy
        import torch
        obs_t = torch.as_tensor(norm_obs, dtype=torch.float32,
                                device=self.model.device)
        with torch.no_grad():
            if self.masked:
                # MaskablePPO policy forward expects action_masks too
                mask_t = torch.as_tensor(mask[np.newaxis, :], dtype=torch.bool,
                                          device=self.model.device)
                dist = self.model.policy.get_distribution(obs_t, action_masks=mask_t)
                value = self.model.policy.predict_values(obs_t)
            else:
                dist = self.model.policy.get_distribution(obs_t)
                value = self.model.policy.predict_values(obs_t)
            entropy = float(dist.entropy().cpu().item())
            v = float(value.cpu().item())
        return int(action[0]), v, entropy


_holder: Optional[ModelHolder] = None


# ---------- FastAPI app ----------
app = FastAPI(title="kubeflux-rl-scheduler", version="0.1")


@app.get("/healthz")
def healthz():
    return {"ready": _holder is not None,
            "masked": _holder.masked if _holder else None,
            "policy_dir": str(_holder.policy_dir) if _holder else None}


@app.post("/act", response_model=ActResponse)
def act(req: ActRequest):
    if _holder is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    obs, mask, top_ids = build_obs(req)
    action, value, entropy = _holder.predict(obs, mask)
    sel_id = top_ids[action] if 0 <= action < TOP_K else None
    return ActResponse(
        action=action,
        selected_job_id=sel_id,
        value=value,
        entropy=entropy,
        top_k_used=sum(1 for x in top_ids if x is not None),
    )


# ---------- entry ----------
def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--policy-dir", required=True)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8002)
    args = p.parse_args(argv)

    global _holder
    _holder = ModelHolder(Path(args.policy_dir))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
