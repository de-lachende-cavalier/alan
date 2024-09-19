from .types import CorePolicyProtocol, GoalCollectorProtocol, GoalReplayBufferProtocol
from typing import Callable, Self, cast
import logging
from dataclasses import asdict

import numpy as np
from tianshou.trainer.base import (
    BaseTrainer,
    OfflineTrainer,
    OffpolicyTrainer,
    OnpolicyTrainer,
)
import tqdm

from tianshou.data import CollectStats, EpochStats, SequenceSummaryStats
from tianshou.data.collector import CollectStatsBase
from tianshou.policy import BasePolicy
from tianshou.trainer.utils import gather_info
from tianshou.utils import (
    BaseLogger,
    DummyTqdm,
    LazyLogger,
    tqdm_config,
)
from tianshou.utils.logging import set_numerical_fields_to_precision

import torch
from .collector import IntrinsicCollectStats
from .policy import CoreTrainingStats

log = logging.getLogger(__name__)


class GoalTrainer(BaseTrainer):
    """GoalTrainer is a goal-aware Tianshou Trainer. It returns an iterator that yields a 3-tuple (epoch, stats, info) for each epoch.

    For details, see https://tianshou.org/en/stable/03_api/trainer/base.html.
    """

    def __init__(
        self,
        policy: CorePolicyProtocol,
        max_epoch: int,
        batch_size: int | None,
        train_collector: GoalCollectorProtocol | None = None,
        test_collector: GoalCollectorProtocol | None = None,
        buffer: GoalReplayBufferProtocol | None = None,
        step_per_epoch: int | None = None,
        repeat_per_collect: int | None = None,
        episode_per_test: int | None = None,
        update_per_step: float = 1.0,
        step_per_collect: int | None = None,
        episode_per_collect: int | None = None,
        train_fn: Callable[[int, int], None] | None = None,
        test_fn: Callable[[int, int | None], None] | None = None,
        stop_fn: Callable[[float], bool] | None = None,
        save_best_fn: Callable[[BasePolicy], None] | None = None,
        save_checkpoint_fn: Callable[[int, int, int], str] | None = None,
        resume_from_log: bool = False,
        reward_metric: Callable[[np.ndarray], np.ndarray] | None = None,
        logger: BaseLogger = LazyLogger(),
        verbose: bool = True,
        show_progress: bool = True,
        test_in_train: bool = True,
        device: torch.device = torch.device("cpu"),
    ):
        super().__init__(
            policy,
            max_epoch,
            batch_size,
            train_collector,
            test_collector,
            buffer,
            step_per_epoch,
            repeat_per_collect,
            episode_per_test,
            update_per_step,
            step_per_collect,
            episode_per_collect,
            train_fn,
            test_fn,
            stop_fn,
            save_best_fn,
            save_checkpoint_fn,
            resume_from_log,
            reward_metric,
            logger,
            verbose,
            show_progress,
            test_in_train,
        )
        self.policy = policy.to(device)
        self.device = device

    def __next__(self) -> EpochStats:
        """Carries out one epoch."""
        self.epoch += 1
        self.iter_num += 1

        if self.iter_num > 1:
            # iterator exhaustion check
            if self.epoch > self.max_epoch:
                raise StopIteration

            # exit flag 1, when stop_fn succeeds in train_step or test_step
            if self.stop_fn_flag:
                raise StopIteration

        progress = tqdm.tqdm if self.show_progress else DummyTqdm

        # perform n step_per_epoch
        with progress(
            total=self.step_per_epoch, desc=f"Epoch #{self.epoch}", **tqdm_config
        ) as t:
            collect_stat: CollectStatsBase
            while t.n < t.total and not self.stop_fn_flag:
                collect_stat, train_stat, self.stop_fn_flag = self.training_step()
                if isinstance(collect_stat, IntrinsicCollectStats):
                    pbar_data_dict = {
                        # total number of steps in the environment
                        "env_step": str(self.env_step),
                        # (fast) intrinsic and extrinsic reward
                        "rew": f"{self.last_rew:.4f}",
                        # (fast) intrinsic reward
                        "int_rew": f"{self.int_rew:.4f}",
                        # episode length, if we completed one episode, else it equals n/st
                        "len": str(int(self.last_len)),
                        # number of episodes seen in one epoch
                        "n/ep": str(collect_stat.n_collected_episodes),
                        # number of steps collected in one epoch
                        "n/st": str(collect_stat.n_collected_steps),
                    }
                    t.update(collect_stat.n_collected_steps)
                else:
                    pbar_data_dict = {}
                    t.update()

                pbar_data_dict = set_numerical_fields_to_precision(pbar_data_dict)
                pbar_data_dict["gradient_step"] = str(self._gradient_step)
                t.set_postfix(**pbar_data_dict)

                if self.stop_fn_flag:
                    break

            if t.n <= t.total and not self.stop_fn_flag:
                t.update()

        # for offline RL
        if self.train_collector is None:
            assert self.buffer is not None
            batch_size = self.batch_size or len(self.buffer)
            self.env_step = self._gradient_step * batch_size

        test_stat = None
        if not self.stop_fn_flag:
            self.logger.save_data(
                self.epoch,
                self.env_step,
                self._gradient_step,
                self.save_checkpoint_fn,
            )
            # test
            if self.test_collector is not None:
                test_stat, self.stop_fn_flag = self.test_step()

        info_stat = gather_info(
            start_time=self.start_time,
            policy_update_time=self.policy_update_time,
            gradient_step=self._gradient_step,
            best_reward=self.best_reward,
            best_reward_std=self.best_reward_std,
            train_collector=self.train_collector,
            test_collector=self.test_collector,
        )

        self.logger.log_info_data(asdict(info_stat), self.epoch)

        # in case trainer is used with run(), epoch_stat will not be returned
        return EpochStats(
            epoch=self.epoch,
            train_collect_stat=collect_stat,
            test_collect_stat=test_stat,
            training_stat=train_stat,
            info_stat=info_stat,
        )

    def _collect_training_data(self) -> CollectStats:
        """Performs training data collection.

        (Note that the training_step() method in __next__() calls this method to do the actual collecting.)
        """
        assert self.episode_per_test is not None
        assert self.train_collector is not None
        if self.train_fn:
            self.train_fn(self.epoch, self.env_step)

        collect_stats = self.train_collector.collect(
            n_step=self.step_per_collect,
            n_episode=self.episode_per_collect,
        )

        self.env_step += collect_stats.n_collected_steps

        # showcase statistics as we step in env, not waiting for full episodes
        if collect_stats.n_collected_steps > 0:
            assert collect_stats.returns_stat is not None  # for mypy
            assert collect_stats.int_returns_stat is not None  # for mypy
            assert collect_stats.lens_stat is not None  # for mypy
            self.last_rew = collect_stats.returns_stat.mean
            self.int_rew = collect_stats.int_returns_stat.mean
            self.last_len = collect_stats.lens_stat.mean
            if self.reward_metric:
                rew = self.reward_metric(collect_stats.returns)
                collect_stats.returns = rew
                collect_stats.returns_stat = SequenceSummaryStats.from_sequence(rew)

            self.logger.log_train_data(asdict(collect_stats), self.env_step)
        return collect_stats

    def to(self, device: torch.device) -> Self:
        self.device = device
        self.policy = self.policy.to(device)
        return self


class GoalOfflineTrainer(OfflineTrainer, GoalTrainer):
    """Offline trainer that works with goals. It samples mini-batches from buffer and passes them to policy.update().

    This implementation is the same as Tianshou's OfflineTrainer. This class exists for conceptual consistency.
    """

    def __init__(self, *args, **kwargs):
        device = kwargs.pop("device", torch.device("cpu"))
        super().__init__(*args, **kwargs)
        self.to(device)


class GoalOffpolicyTrainer(OffpolicyTrainer, GoalTrainer):
    """Offpolicy trainer that works with goals. It samples mini-batches from buffer and passes them to policy.update().

    This implementation is the same as Tianshou's OffpolicyTrainer. This class exists for conceptual consistency.
    """

    def __init__(self, *args, **kwargs):
        device = kwargs.pop("device", torch.device("cpu"))
        super().__init__(*args, **kwargs)
        self.to(device)


class GoalOnpolicyTrainer(OnpolicyTrainer, GoalTrainer):
    """Onpolicy trainer that works with goals. It passes the entire buffer to policy.update() and resets it afterwards.

    This implementation is the same as Tianshou's OnpolicyTrainer. This class exists for conceptual consistency.
    """

    def __init__(self, *args, **kwargs):
        device = kwargs.pop("device", torch.device("cpu"))
        super().__init__(*args, **kwargs)
        self.to(device)

    def policy_update_fn(
        self,
        result: CollectStatsBase | None = None,
    ) -> CoreTrainingStats:
        """Performs one on-policy update by passing the entire buffer to the policy's update method."""
        assert self.train_collector is not None
        training_stat = self.policy.update(
            sample_size=0,
            buffer=self.train_collector.buffer,
            batch_size=self.batch_size,
            repeat=self.repeat_per_collect,
        )

        # just for logging, no functional role
        self.policy_update_time += training_stat.train_time

        # this is the main difference to the off-policy trainer
        self.train_collector.reset_buffer(keep_statistics=True)

        self._update_moving_avg_stats_and_log_update_data(training_stat.policy_stats)

        return training_stat
