from typing import Any, cast
from .types import GoalBatchProtocol

import numpy as np

from tianshou.data import ReplayBuffer, ReplayBufferManager, Batch


class GoalReplayBuffer(ReplayBuffer):
    """The Buffer stores the data generated from the interaction between policy and environment.

    For details, see https://tianshou.org/en/stable/03_api/data/buffer/base.html. On top of Tianshou's implementation, we add the possibility of storing latent goals.
    """

    _reserved_keys = (
        "obs",
        "latent_goal",
        "act",
        "obs_next",
        "latent_goal_next",
        "rew",
        "terminated",
        "truncated",
        "done",
        "info",
        "policy",
    )

    _input_keys = (
        "obs",
        "latent_goal",
        "act",
        "obs_next",
        "latent_goal_next",
        "rew",
        "terminated",
        "truncated",
        "info",
        "policy",
    )

    def __getitem__(
        self, index: slice | int | list[int] | np.ndarray
    ) -> GoalBatchProtocol:
        if isinstance(index, slice):  # change slice to np array
            # buffer[:] will get all available data
            indices = (
                self.sample_indices(0)
                if index == slice(None)
                else self._indices[: len(self)][index]
            )
        else:
            indices = index  # type: ignore

        # raise KeyError first instead of AttributeError,
        # to support np.array([ReplayBuffer()])
        obs = self.get(indices, "obs")

        if self._save_obs_next:
            obs_next = self.get(indices, "obs_next", Batch())
            latent_goal_next = self.get(indices, "latent_goal_next", Batch())
        else:
            obs_next = self.get(self.next(indices), "obs", Batch())
            latent_goal_next = self.get(indices, "latent_goal", Batch())

        batch_dict = {
            "obs": obs,
            "latent_goal": self.latent_goal[indices],
            "act": self.act[indices],
            "obs_next": obs_next,
            "latent_goal_next": latent_goal_next,
            "rew": self.rew[indices],
            "terminated": self.terminated[indices],
            "truncated": self.truncated[indices],
            "done": self.done[indices],
            "info": self.get(indices, "info", Batch()),
            "policy": self.get(indices, "policy", Batch()),
        }

        for key in self._meta.__dict__:
            if key not in self._input_keys:
                batch_dict[key] = self._meta[key][indices]
        return cast(GoalBatchProtocol, Batch(batch_dict))


class GoalReplayBufferManager(ReplayBufferManager, GoalReplayBuffer):
    """GoalReplayBufferManager contains a list of GoalReplayBuffers, each with the exact same configuration.

    For details, see https://tianshou.org/en/stable/03_api/data/buffer/manager.html."""

    def __init__(self, buffer_list: list[GoalReplayBuffer]) -> None:
        super().__init__(buffer_list)

    def add(
        self,
        batch: GoalBatchProtocol,
        buffer_ids: np.ndarray | list[int] | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        return ReplayBufferManager.add(self, batch, buffer_ids)


class GoalVectorReplayBuffer(GoalReplayBufferManager):
    """The GoalVectorReplayBuffer contains `n` GoalReplayBuffers of the same size.

    For details, see https://tianshou.org/en/stable/03_api/data/buffer/vecbuf.html."""

    def __init__(self, total_size: int, buffer_num: int, **kwargs: Any) -> None:
        assert buffer_num > 0
        size = int(np.ceil(total_size / buffer_num))
        buffer_list = [GoalReplayBuffer(size, **kwargs) for _ in range(buffer_num)]
        super().__init__(buffer_list)
