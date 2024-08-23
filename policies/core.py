from collections.abc import Callable
from typing import Literal
from abc import abstractmethod

import gymnasium as gym

import numpy as np
from tianshou.data import ReplayBuffer
from tianshou.data.batch import BatchProtocol
from tianshou.data.types import (
    ActBatchProtocol,
    ActStateBatchProtocol,
    BatchWithReturnsProtocol,
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.policy import BasePolicy
from tianshou.policy.base import (
    TLearningRateScheduler,
    TrainingStatsWrapper,
)
from torch import Tensor

from models import SelfModel, EnvModel


class CoreTrainingStats(TrainingStatsWrapper):
    def __init__(self, wrapped_stats):
        # TODO should I add more to this?
        super().__init__(wrapped_stats)


class CorePolicy(BasePolicy[CoreTrainingStats]):
    def __init__(
        self,
        *,
        self_model: SelfModel,
        env_model: EnvModel,
        action_space: gym.Space,
        observation_space: gym.Space | None,
        action_scaling: bool = False,
        action_bound_method: None | Literal["clip"] | Literal["tanh"] = "clip",
        lr_scheduler: TLearningRateScheduler | None = None,
    ) -> None:
        super().__init__(
            action_space=action_space,
            observation_space=observation_space,
            action_scaling=action_scaling,
            action_bound_method=action_bound_method,
            lr_scheduler=lr_scheduler,
        )

        self.self_model = self_model
        self.env_model = env_model

    def combine_reward(self, batch: RolloutBatchProtocol, beta: float) -> np.ndarray:
        """Combines the intrinsic and extrinsic rewards into a single scalar value."""
        i_rew = self.self_model(batch)
        return batch.rew + beta * i_rew

    def process_fn(
        self,
        batch: RolloutBatchProtocol,
        buffer: ReplayBuffer,
        indices: np.ndarray,
        beta: float = 0.314,
    ) -> RolloutBatchProtocol:
        # it is sufficient to call combine_reward here because process_fn() gets called before all the learning happens
        self.combine_reward(batch, beta)

        return super().process_fn(batch, buffer, indices)
