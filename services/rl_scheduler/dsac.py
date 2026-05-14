"""Discrete Soft Actor-Critic with action masking (DSAC).

Policy is implicit: π(a|s) = softmax(min(Q1,Q2)(s,·) / α), no separate actor.
Twin Q-networks with LayerNorm (RLPD-recommended for stable offline+online mixing).
Temperature α auto-tuned via gradient on log_α.

Two Q-network architectures available via use_attention flag:
  MLP  (default): flat concat of all obs dims → twin MLP Q-network.
  Attn (this branch): self-attention over job queue tokens (permutation-
        invariant) + linear cluster encoder → fused Q-head.

References:
  Christodoulou 2019, "Soft Actor-Critic for Discrete Action Spaces"
  Kool et al. 2019 ICLR, "Attention, Learn to Solve Routing Problems!"
  Lee et al. 2019 ICML, "Set Transformer"
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, Sequence

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


def _build_mlp(in_dim: int, hidden: Sequence[int], out_dim: int,
               layer_norm: bool = True) -> nn.Sequential:
    layers: list[nn.Module] = []
    prev = in_dim
    for h in hidden:
        layers.append(nn.Linear(prev, h))
        if layer_norm:
            layers.append(nn.LayerNorm(h))
        layers.append(nn.ReLU())
        prev = h
    layers.append(nn.Linear(prev, out_dim))
    for m in layers:
        if isinstance(m, nn.Linear):
            nn.init.orthogonal_(m.weight, gain=1.0)
            nn.init.zeros_(m.bias)
    return nn.Sequential(*layers)


class _QNet(nn.Module):
    """Flat MLP Q-network (baseline)."""
    def __init__(self, obs_dim: int, n_actions: int,
                 hidden: Sequence[int], layer_norm: bool) -> None:
        super().__init__()
        self.net = _build_mlp(obs_dim, hidden, n_actions, layer_norm)

    def forward(self, obs: torch.Tensor) -> torch.Tensor:
        return self.net(obs)


class _AttentionQNet(nn.Module):
    """Q-network with self-attention over the job queue (permutation-invariant).

    Observation layout (must match gym_env.py _build_obs):
        obs = [job_0 (JOB_DIM), ..., job_{K-1} (JOB_DIM), cluster (obs_dim - K*JOB_DIM)]

    Architecture:
        1. job_embed:    Linear(JOB_DIM → d_model) + ReLU
        2. transformer:  TransformerEncoder (pre-LN, batch_first)
           → mean-pool non-padding tokens → queue_ctx (d_model)
        3. cluster_embed: Linear(cluster_dim → d_model) + ReLU → cluster_ctx
        4. q_head:       MLP([queue_ctx ‖ cluster_ctx] → n_actions)

    Padding mask: job slots where all features == 0 are treated as padding
    and excluded from the mean-pool (handles variable-length queues).
    No positional encoding — job order is irrelevant for scheduling.
    """

    TOP_K: int = 16   # must match gym_env.TOP_K
    JOB_DIM: int = 11  # features per job slot (must match gym_env.py)

    def __init__(
        self,
        obs_dim: int,
        n_actions: int,
        d_model: int = 64,
        n_heads: int = 4,
        n_layers: int = 2,
        layer_norm: bool = True,
    ) -> None:
        super().__init__()
        self.top_k = self.TOP_K
        self.job_dim = self.JOB_DIM
        self.cluster_dim = obs_dim - self.TOP_K * self.JOB_DIM
        assert self.cluster_dim > 0, (
            f"obs_dim={obs_dim} too small for TOP_K={self.TOP_K} × JOB_DIM={self.JOB_DIM}"
        )

        self.job_embed = nn.Linear(self.JOB_DIM, d_model)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_heads,
            dim_feedforward=d_model * 2,
            dropout=0.0, batch_first=True,
            norm_first=True,   # pre-LN: more stable than post-LN
        )
        # enable_nested_tensor=False: norm_first=True doesn't support nested
        # tensors; disabling avoids a PyTorch UserWarning with no perf impact
        # on our fixed-size (B, TOP_K, d_model) input.
        self.transformer = nn.TransformerEncoder(
            encoder_layer, num_layers=n_layers, enable_nested_tensor=False,
        )
        self.cluster_embed = nn.Linear(self.cluster_dim, d_model)
        self.q_head = _build_mlp(d_model * 2, (d_model,), n_actions, layer_norm)

        for m in [self.job_embed, self.cluster_embed]:
            nn.init.orthogonal_(m.weight, gain=1.0)
            nn.init.zeros_(m.bias)

    def forward(self, obs: torch.Tensor) -> torch.Tensor:
        # Split obs into job tokens and cluster state
        job_flat = obs[:, :self.top_k * self.job_dim]          # (B, K*11)
        cluster  = obs[:, self.top_k * self.job_dim:]           # (B, cluster_dim)

        jobs = job_flat.view(-1, self.top_k, self.job_dim)      # (B, K, 11)

        # Padding mask: slots where all features == 0 are empty queue entries
        pad_mask = (jobs.abs().sum(dim=-1) == 0)                # (B, K)

        job_tok = F.relu(self.job_embed(jobs))                  # (B, K, d)
        job_enc = self.transformer(job_tok,
                                   src_key_padding_mask=pad_mask)  # (B, K, d)

        # Mean-pool over non-padding tokens → permutation-invariant queue ctx
        non_pad = (~pad_mask).float().unsqueeze(-1)             # (B, K, 1)
        n_valid = non_pad.sum(dim=1).clamp(min=1.0)            # (B, 1)
        queue_ctx = (job_enc * non_pad).sum(dim=1) / n_valid   # (B, d)

        cluster_ctx = F.relu(self.cluster_embed(cluster))       # (B, d)

        fused = torch.cat([queue_ctx, cluster_ctx], dim=-1)    # (B, 2d)
        return self.q_head(fused)                              # (B, n_actions)


class DSACAgent:
    """Discrete SAC agent for masked scheduling environments.

    Two Q-network backends selectable via use_attention:
      False (default on DSAC branch): flat MLP, hidden=(256,256)
      True  (DSAC-attention branch):  self-attention over job queue tokens

    Usage::
        agent = DSACAgent(obs_dim=192, n_actions=17, use_attention=True)
        act = agent.select_action(obs, mask)
        losses = agent.update(batch)
    """

    def __init__(
        self,
        obs_dim: int,
        n_actions: int,
        hidden: Sequence[int] = (256, 256),
        lr_q: float = 3e-4,
        lr_alpha: float = 3e-4,
        gamma: float = 0.99,
        tau: float = 0.005,
        init_alpha: float = 0.1,
        target_entropy_ratio: float = 0.1,
        fixed_alpha: bool = True,
        layer_norm: bool = True,
        use_attention: bool = True,   # True = attention Q-net on this branch
        attn_d_model: int = 64,
        attn_n_heads: int = 4,
        attn_n_layers: int = 2,
        device: str = "cpu",
    ) -> None:
        self.obs_dim = obs_dim
        self.n_actions = n_actions
        self.gamma = gamma
        self.tau = tau
        self.use_attention = use_attention
        self.device = torch.device(device)

        def _make_q():
            if use_attention:
                return _AttentionQNet(obs_dim, n_actions,
                                      d_model=attn_d_model,
                                      n_heads=attn_n_heads,
                                      n_layers=attn_n_layers,
                                      layer_norm=layer_norm).to(self.device)
            return _QNet(obs_dim, n_actions, hidden, layer_norm).to(self.device)

        self.q1 = _make_q()
        self.q2 = _make_q()
        self.q1_target = _make_q()
        self.q2_target = _make_q()
        self.q1_target.load_state_dict(self.q1.state_dict())
        self.q2_target.load_state_dict(self.q2.state_dict())

        self.fixed_alpha = fixed_alpha
        self.log_alpha = torch.tensor(
            math.log(init_alpha), dtype=torch.float32,
            requires_grad=not fixed_alpha, device=self.device,
        )
        self.target_entropy_ratio = target_entropy_ratio
        self.target_entropy = target_entropy_ratio * math.log(n_actions)

        self.opt_q = torch.optim.Adam(
            list(self.q1.parameters()) + list(self.q2.parameters()), lr=lr_q)
        self.opt_alpha = torch.optim.Adam([self.log_alpha], lr=lr_alpha) \
            if not fixed_alpha else None

        self._update_count = 0

    # ------------------------------------------------------------------
    @property
    def alpha(self) -> torch.Tensor:
        return self.log_alpha.exp()

    def _masked_policy(
        self, q_vals: torch.Tensor, mask: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """Masked softmax → (probs, log_probs), shape (B, n_actions)."""
        logits = q_vals / self.alpha.detach().clamp(min=1e-8)
        logits = logits.masked_fill(~mask, -1e9)
        probs = F.softmax(logits, dim=-1)
        log_probs = F.log_softmax(logits, dim=-1)
        # Zero out log-probs for masked actions (avoid -inf in entropy)
        log_probs = log_probs.masked_fill(~mask, 0.0)
        return probs, log_probs

    def _soft_value(
        self,
        obs: torch.Tensor,
        mask: torch.Tensor,
        use_target: bool = True,
    ) -> torch.Tensor:
        """V_soft(s) = Σ_a π(a|s) [min_Q(s,a) − α log π(a|s)]."""
        if use_target:
            q = torch.min(self.q1_target(obs), self.q2_target(obs))
        else:
            q = torch.min(self.q1(obs), self.q2(obs))
        probs, log_probs = self._masked_policy(q, mask)
        return (probs * (q - self.alpha.detach() * log_probs)).sum(dim=-1)

    # ------------------------------------------------------------------
    def update(self, batch: Dict[str, np.ndarray]) -> Dict[str, float]:
        """One gradient step. batch must contain:
        obs, acts, rews, next_obs, dones, masks, next_masks."""
        def _t(k, dtype=torch.float32):
            return torch.as_tensor(batch[k], dtype=dtype, device=self.device)

        obs = _t("obs")
        acts = _t("acts", torch.long)
        rews = _t("rews")
        next_obs = _t("next_obs")
        dones = _t("dones", torch.float32)
        masks = _t("masks", torch.bool)
        next_masks = _t("next_masks", torch.bool)
        # gammas holds γ^n for n-step returns (γ^1 for 1-step)
        gammas = _t("gammas") if "gammas" in batch else \
            torch.full_like(rews, self.gamma)

        # ---- Critic update -------------------------------------------
        with torch.no_grad():
            v_next = self._soft_value(next_obs, next_masks, use_target=True)
            target_q = rews + gammas * (1.0 - dones) * v_next

        q1_a = self.q1(obs).gather(1, acts.unsqueeze(1)).squeeze(1)
        q2_a = self.q2(obs).gather(1, acts.unsqueeze(1)).squeeze(1)
        loss_q = F.mse_loss(q1_a, target_q) + F.mse_loss(q2_a, target_q)

        self.opt_q.zero_grad()
        loss_q.backward()
        nn.utils.clip_grad_norm_(
            list(self.q1.parameters()) + list(self.q2.parameters()), 10.0)
        self.opt_q.step()

        # Soft-update targets
        self._update_count += 1
        for src, tgt in [(self.q1, self.q1_target), (self.q2, self.q2_target)]:
            for p, pt in zip(src.parameters(), tgt.parameters()):
                pt.data.mul_(1.0 - self.tau).add_(p.data, alpha=self.tau)

        # ---- Temperature update (skipped when fixed_alpha=True) --------
        loss_alpha_val = 0.0
        entropy_val = float("nan")
        target_entropy_val = float("nan")
        n_valid_val = float("nan")

        if not self.fixed_alpha:
            with torch.no_grad():
                q_avg = (self.q1(obs) + self.q2(obs)) * 0.5
                probs, log_probs = self._masked_policy(q_avg, masks)
            entropy = -(probs * log_probs).sum(dim=-1).mean()

            n_valid = masks.float().sum(dim=-1).clamp(min=1.0).mean()
            target_entropy = self.target_entropy_ratio * torch.log(n_valid)

            loss_alpha = self.log_alpha * (entropy - target_entropy).detach()
            self.opt_alpha.zero_grad()
            loss_alpha.backward()
            self.opt_alpha.step()

            with torch.no_grad():
                prev = self.log_alpha.item()
                self.log_alpha.clamp_(-5.0, 0.5)
                if abs(self.log_alpha.item() - prev) > 1e-6:
                    self.opt_alpha.state[self.log_alpha] = {}

            loss_alpha_val = loss_alpha.item()
            entropy_val = entropy.item()
            target_entropy_val = target_entropy.item()
            n_valid_val = n_valid.item()

        return {
            "loss_q": loss_q.item(),
            "loss_alpha": loss_alpha_val,
            "alpha": self.alpha.item(),
            "entropy": entropy_val,
            "target_entropy": target_entropy_val,
            "n_valid_actions": n_valid_val,
        }

    # ------------------------------------------------------------------
    def select_action(
        self, obs: np.ndarray, mask: np.ndarray, greedy: bool = False
    ) -> int:
        with torch.no_grad():
            obs_t = torch.as_tensor(obs, dtype=torch.float32,
                                    device=self.device).unsqueeze(0)
            mask_t = torch.as_tensor(mask, dtype=torch.bool,
                                     device=self.device).unsqueeze(0)
            q = torch.min(self.q1(obs_t), self.q2(obs_t))
            probs, _ = self._masked_policy(q, mask_t)
            probs_np = probs.squeeze(0).cpu().numpy()
        probs_np = probs_np * mask.astype(np.float32)
        total = probs_np.sum()
        if total < 1e-9:
            return int(np.flatnonzero(mask)[0])
        probs_np /= total
        if greedy:
            return int(probs_np.argmax())
        return int(np.random.choice(len(probs_np), p=probs_np))

    # ------------------------------------------------------------------
    def save(self, path: str | Path) -> None:
        torch.save({
            "q1": self.q1.state_dict(),
            "q2": self.q2.state_dict(),
            "q1_target": self.q1_target.state_dict(),
            "q2_target": self.q2_target.state_dict(),
            "opt_q": self.opt_q.state_dict(),
            "log_alpha": self.log_alpha.item(),
            "opt_alpha": self.opt_alpha.state_dict() if self.opt_alpha else None,
            "fixed_alpha": self.fixed_alpha,
            "use_attention": self.use_attention,
            "obs_dim": self.obs_dim,
            "n_actions": self.n_actions,
            "update_count": self._update_count,
        }, str(path))

    @classmethod
    def load(cls, path: str | Path, **kwargs) -> "DSACAgent":
        data = torch.load(str(path), map_location="cpu", weights_only=False)
        fixed_alpha = data.get("fixed_alpha", False)
        use_attention = data.get("use_attention", False)
        agent = cls(obs_dim=data["obs_dim"], n_actions=data["n_actions"],
                    fixed_alpha=fixed_alpha, use_attention=use_attention,
                    **kwargs)
        agent.q1.load_state_dict(data["q1"])
        agent.q2.load_state_dict(data["q2"])
        agent.q1_target.load_state_dict(data["q1_target"])
        agent.q2_target.load_state_dict(data["q2_target"])
        agent.opt_q.load_state_dict(data["opt_q"])
        with torch.no_grad():
            agent.log_alpha.fill_(float(data["log_alpha"]))
        if agent.opt_alpha and data.get("opt_alpha"):
            agent.opt_alpha.load_state_dict(data["opt_alpha"])
        agent._update_count = data["update_count"]
        return agent
