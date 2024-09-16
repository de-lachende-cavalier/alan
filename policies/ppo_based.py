from typing import Any, Literal, cast, Self
from core.types import (
    GoalBatchProtocol,
    SelfModelProtocol,
    EnvModelProtocol,
)

from tianshou.data import ReplayBuffer, to_torch_as
from tianshou.data.batch import BatchProtocol
from tianshou.data.types import ObsBatchProtocol, BatchWithAdvantagesProtocol
from tianshou.policy.modelfree.ppo import PPOPolicy, TPPOTrainingStats
from tianshou.policy.base import TLearningRateScheduler

import numpy as np
import gymnasium as gym

import torch

from networks import GoalNetHackActor, GoalNetHackCritic
from core import CorePolicy


class PPOBasedPolicy(CorePolicy):
    """A policy combining a Tianshou PPOPolicy and the CorePolicy.

    It is mostly meant as a blueprint/example for future integration of my code with Tianshou.
    """

    # TODO no lifelong learning yet!

    def __init__(
        self,
        *,
        self_model: SelfModelProtocol,
        env_model: EnvModelProtocol,
        act_net: GoalNetHackActor,
        critic_net: GoalNetHackCritic,
        optim: torch.optim.Optimizer,
        action_space: gym.Space,
        observation_space: gym.Space | None,
        action_scaling: bool = False,
        action_bound_method: None | Literal["clip"] | Literal["tanh"] = "clip",
        lr_scheduler: TLearningRateScheduler | None = None,
        device: torch.device = torch.device("cpu"),
    ) -> None:
        super().__init__(
            self_model=self_model,
            env_model=env_model,
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=action_scaling,
            action_bound_method=action_bound_method,
            lr_scheduler=lr_scheduler,
            device=device,
        )

        self.ppo_policy = PPOPolicy(
            actor=act_net.to(device),
            critic=critic_net.to(device),
            optim=optim,
            action_space=action_space,
            # we can confidently hardcode these two because we intend to use NLE
            dist_fn=self._dist_fn,
            action_scaling=False,
        )
        self.device = device

    def learn(
        self,
        batch: GoalBatchProtocol,
        batch_size: int | None,
        repeat: int,
        *args: Any,
        **kwargs: Any,
    ) -> TPPOTrainingStats:
        return self.ppo_policy.learn(batch, batch_size, repeat, *args, **kwargs)

    def forward(
        self,
        batch: ObsBatchProtocol,
        state: dict | BatchProtocol | np.ndarray | None = None,
        **kwargs: Any,
    ) -> GoalBatchProtocol:
        latent_goal = super().forward(batch, state)
        # this is somewhat hacky, but it provides a cleaner interface with Tianshou
        batch.obs["latent_goal"] = latent_goal

        result = self.ppo_policy.forward(batch, state, **kwargs)
        result.latent_goal = latent_goal
        return cast(GoalBatchProtocol, result)

    def process_fn(
        self,
        batch: GoalBatchProtocol,
        buffer: ReplayBuffer,
        indices: np.ndarray,
    ) -> GoalBatchProtocol:
        batch = super().process_fn(batch, buffer, indices)
        batch.obs["latent_goal"] = batch.latent_goal
        # one goal per observation
        batch.obs_next["latent_goal"] = batch.latent_goal_next
        # TODO hacky, find a more elegant solution (for MPS)
        self.ppo_policy._compute_returns = self._compute_returns
        return self.ppo_policy.process_fn(batch, buffer, indices)

    def _compute_returns(
        self,
        batch: GoalBatchProtocol,
        buffer: ReplayBuffer,
        indices: np.ndarray,
    ) -> BatchWithAdvantagesProtocol:
        v_s, v_s_ = [], []
        with torch.no_grad():
            for minibatch in batch.split(
                self.ppo_policy.max_batchsize, shuffle=False, merge_last=True
            ):
                v_s.append(self.ppo_policy.critic(minibatch.obs))
                v_s_.append(self.ppo_policy.critic(minibatch.obs_next))
        batch.v_s = torch.cat(v_s, dim=0).flatten()  # old value
        v_s = batch.v_s.cpu().numpy()
        v_s_ = torch.cat(v_s_, dim=0).flatten().cpu().numpy()
        if self.ppo_policy.rew_norm:  # unnormalize v_s & v_s_
            v_s = v_s * np.sqrt(self.ppo_policy.ret_rms.var + self.ppo_policy._eps)
            v_s_ = v_s_ * np.sqrt(self.ppo_policy.ret_rms.var + self.ppo_policy._eps)
        unnormalized_returns, advantages = self.compute_episodic_return(
            batch,
            buffer,
            indices,
            v_s_,
            v_s,
            gamma=self.ppo_policy.gamma,
            gae_lambda=self.ppo_policy.gae_lambda,
        )
        # these two lines are the only different ones!
        unnormalized_returns = unnormalized_returns.astype(np.float32)
        advantages = advantages.astype(np.float32)
        if self.ppo_policy.rew_norm:
            batch.returns = unnormalized_returns / np.sqrt(
                self.ppo_policy.ret_rms.var + self.ppo_policy._eps
            )
            self.ret_rms.update(unnormalized_returns)
        else:
            batch.returns = unnormalized_returns
        batch.returns = to_torch_as(batch.returns, batch.v_s)
        batch.adv = to_torch_as(advantages, batch.v_s)
        return cast(BatchWithAdvantagesProtocol, batch)

    def _dist_fn(self, logits: torch.Tensor):
        return torch.distributions.Categorical(logits=logits)

    def to(self, device: torch.device) -> Self:
        super().to(device)
        self.device = device
        self.ppo_policy.actor = self.ppo_policy.actor.to(device)
        self.ppo_policy.critic = self.ppo_policy.critic.to(device)
        return self
