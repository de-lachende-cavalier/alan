from typing import (
    Protocol,
    TypeVar,
    Any,
    Union,
    List,
    Dict,
    Optional,
    Literal,
    Callable,
)
from tianshou.data.types import (
    RolloutBatchProtocol,
    ObsBatchProtocol,
    ActStateBatchProtocol,
    ActBatchProtocol,
)
from tianshou.data import (
    Batch,
    ReplayBuffer,
    CollectStats,
    EpochStats,
)
from tianshou.data.batch import BatchProtocol, TArr
from tianshou.policy import BasePolicy
from tianshou.policy.base import (
    TLearningRateScheduler,
    TrainingStatsWrapper,
    TrainingStats,
)
from tianshou.trainer.base import BaseTrainer
from tianshou.utils import BaseLogger, LazyLogger

import torch
from torch import nn
import numpy as np
import gymnasium as gym

# TODO a few types in this file need to be updated
# TODO there might be a few places where I'm supposed to use types in this file, but I don't!

TArrLike = TypeVar("TArrLike", np.ndarray, torch.Tensor, Batch, None)


class ObsActNextBatchProtocol(BatchProtocol, Protocol):
    """A BatchProtocol containing an observation, an action, and the observation after it.

    Usually used by the intrinsic module and obtained from the Collector.
    """

    obs: TArr | BatchProtocol
    act: TArr
    obs_next: TArr | BatchProtocol


class IntrinsicBatchProtocol(RolloutBatchProtocol, Protocol):
    """A RolloutBatchProtocol with added intrinsic rewards.

    For details on RolloutBatchProtocol, see https://tianshou.org/en/stable/_modules/tianshou/data/types.html.
    """

    int_rew: np.ndarray


class GoalBatchProtocol(IntrinsicBatchProtocol, Protocol):
    """An IntrinsicBatchProtocol with latent goals for the current and the next observation.

    Usually obtained form sampling a GoalReplayBuffer.
    """

    latent_goal: np.ndarray
    latent_goal_next: np.ndarray


# this type should come in handy if I want to experiment with different observation net architectures
class ObservationNetProtocol(Protocol):
    def __init__(
        self,
        observation_space: gym.Space,
        embedding_dim: int = 32,
        crop_dim: int = 9,
        num_layers: int = 5,
    ) -> None: ...

    def forward(
        self, env_out_batch: Dict[str, torch.Tensor | np.ndarray]
    ) -> torch.Tensor: ...


RB = TypeVar("RB", bound=ReplayBuffer)


class GoalReplayBufferProtocol(Protocol[RB]):
    _reserved_keys: tuple
    _input_keys: tuple

    def __getitem__(
        self, index: Union[slice, int, List[int], np.ndarray]
    ) -> GoalBatchProtocol: ...


# TODO
class FastIntrinsicModuleProtocol(Protocol): ...


# TODO
class SlowIntrinsicModuleProtocol(Protocol): ...


class SelfModelProtocol(Protocol):
    obs_net: ObservationNetProtocol
    buffer: GoalReplayBufferProtocol
    intrinsic_module: nn.Module

    def __init__(
        self,
        obs_net: ObservationNetProtocol,
        action_space: gym.Space,
        buffer: GoalReplayBufferProtocol,
        intrinsic_module: nn.Module,
        her_horizon: int,
    ) -> None: ...

    @torch.no_grad()
    def select_goal(self, batch_obs: ObsBatchProtocol) -> torch.Tensor: ...

    @torch.no_grad()
    def fast_intrinsic_reward(
        self, obs: TArrLike, act: np.ndarray, obs_next: TArrLike
    ) -> np.ndarray: ...

    @torch.no_grad()
    def slow_intrinsic_reward_(self, batch_size: int) -> np.ndarray: ...

    def __call__(self, batch: GoalBatchProtocol, sleep: bool = False) -> None: ...


# TODO
class EnvModelProtocol(Protocol): ...


TW = TypeVar("TS", bound=TrainingStatsWrapper)


class CoreTrainingStatsProtocol(Protocol[TW]):
    def __init__(self, wrapped_stats: TrainingStats): ...


TS = TypeVar("TS", bound=CoreTrainingStatsProtocol)
BP = TypeVar("BP", bound=BasePolicy[TS])


class CorePolicyProtocol(Protocol[BP]):
    self_model: SelfModelProtocol
    env_model: EnvModelProtocol
    _beta: float

    def __init__(
        self,
        *,
        self_model: SelfModelProtocol,
        env_model: EnvModelProtocol,
        action_space: gym.Space,
        observation_space: Optional[gym.Space],
        action_scaling: bool = False,
        action_bound_method: Optional[Literal["clip", "tanh"]] = "clip",
        lr_scheduler: Optional[TLearningRateScheduler] = None,
        beta0: float = 0.314,
    ) -> None: ...

    @property
    def beta(self) -> float: ...

    @beta.setter
    def beta(self, value: float) -> None: ...

    def get_beta(self) -> float: ...

    def combine_fast_reward(
        self, rew: np.ndarray, int_rew: np.ndarray
    ) -> np.ndarray: ...

    def combine_slow_reward_(self, batch: GoalBatchProtocol) -> np.ndarray: ...

    def forward(
        self,
        batch: ObsBatchProtocol,
        state: Optional[Union[Dict, BatchProtocol, np.ndarray]] = None,
        **kwargs: Any,
    ) -> Union[ActBatchProtocol, ActStateBatchProtocol]: ...

    def process_fn(
        self,
        batch: GoalBatchProtocol,
        buffer: GoalReplayBufferProtocol,
        indices: np.ndarray,
    ) -> GoalBatchProtocol: ...


CS = TypeVar("CS", bound=CollectStats)


class GoalCollectStatsProtocol(Protocol[CS]):
    int_returns: np.ndarray

    @classmethod
    def with_autogenerated_stats(
        cls,
        returns: np.ndarray,
        int_returns: np.ndarray,
        lens: np.ndarray,
        n_collected_episodes: int = 0,
        n_collected_steps: int = 0,
        collect_time: float = 0.0,
        collect_speed: float = 0.0,
    ) -> "GoalCollectStatsProtocol": ...


class GoalCollectorProtocol(Protocol):
    def __init__(
        self,
        policy: CorePolicyProtocol,
        env: Union[gym.Env, gym.vector.VectorEnv],
        buffer: Optional[GoalReplayBufferProtocol] = None,
        exploration_noise: bool = False,
    ) -> None: ...

    def _collect(
        self,
        n_step: Optional[int] = None,
        n_episode: Optional[int] = None,
        random: bool = False,
        render: Optional[float] = None,
        gym_reset_kwargs: Optional[Dict[str, Any]] = None,
    ) -> GoalCollectStatsProtocol: ...

    def _compute_action_policy_hidden(
        self,
        random: bool,
        ready_env_ids_R: np.ndarray,
        last_obs_RO: np.ndarray,
        last_info_R: np.ndarray,
        last_hidden_state_RH: Optional[Union[np.ndarray, torch.Tensor, Batch]] = None,
    ) -> tuple[
        np.ndarray, np.ndarray, Batch, Optional[Union[np.ndarray, torch.Tensor, Batch]]
    ]: ...


BT = TypeVar("BT", bound=BaseTrainer)


class GoalTrainerProtocol(Protocol[BT]):
    def __init__(
        self,
        policy: CorePolicyProtocol,
        max_epoch: int,
        batch_size: Optional[int],
        train_collector: Optional[GoalCollectorProtocol] = None,
        test_collector: Optional[GoalCollectorProtocol] = None,
        buffer: Optional[GoalReplayBufferProtocol] = None,
        step_per_epoch: Optional[int] = None,
        repeat_per_collect: Optional[int] = None,
        episode_per_test: Optional[int] = None,
        update_per_step: float = 1.0,
        step_per_collect: Optional[int] = None,
        episode_per_collect: Optional[int] = None,
        train_fn: Optional[Callable[[int, int], None]] = None,
        test_fn: Optional[Callable[[int, Optional[int]], None]] = None,
        stop_fn: Optional[Callable[[float], bool]] = None,
        save_best_fn: Optional[Callable[[CorePolicyProtocol], None]] = None,
        save_checkpoint_fn: Optional[Callable[[int, int, int], str]] = None,
        resume_from_log: bool = False,
        reward_metric: Optional[Callable[[np.ndarray], np.ndarray]] = None,
        logger: BaseLogger = LazyLogger(),
        verbose: bool = True,
        show_progress: bool = True,
        test_in_train: bool = True,
    ) -> None: ...

    def __next__(self) -> EpochStats: ...

    def _collect_training_data(self) -> CollectStats: ...
