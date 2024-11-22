from typing import Any, List, Optional, cast
from core.types import KBBatchProtocol

from tianshou.data import ReplayBuffer, ReplayBufferManager, Batch
from tianshou.data.batch import alloc_by_keys_diff, create_value
import numpy as np


class KnowledgeBase(ReplayBuffer):
    """A replay buffer that represents the agent's knowledge base.

    It stores transitions and aggregates them into trajectories.
    """

    # (latent observation, action, reward, trajectory identifier)
    _reserved_keys = ("latent_obs", "act", "rew", "traj_id")
    _input_keys = ("latent_obs", "act", "rew", "traj_id")

    def __init__(
        self,
        size: int,
        stack_num: int = 1,
        ignore_obs_next: bool = True,  # we do not need the obs_next
        save_only_last_obs: bool = True,  # we only need the last observation
        sample_avail: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            size, stack_num, ignore_obs_next, save_only_last_obs, sample_avail, **kwargs
        )

    def __getitem__(
        self, index: slice | int | list[int] | np.ndarray
    ) -> KBBatchProtocol:
        if isinstance(index, slice):  # change slice to np array
            indices = (
                self.sample_indices(0)
                if index == slice(None)
                else self._indices[: len(self)][index]
            )
        else:
            indices = index  # type: ignore

        # raise KeyError first instead of AttributeError,
        # to support np.array([ReplayBuffer()])
        latent_obs = self.get(indices, "latent_obs")

        batch_dict = {
            "latent_obs": latent_obs,
            "act": self.act[indices],
            "rew": self.rew[indices],
            "traj_id": self.traj_id[indices],
        }

        for key in self._meta.__dict__:
            if key not in self._input_keys:
                batch_dict[key] = self._meta[key][indices]
        return cast(KBBatchProtocol, Batch(batch_dict))


class KnowledgeBaseManager(KnowledgeBase, ReplayBufferManager):
    """A class for managing vectorised knowledge bases."""

    def __init__(self, buffer_list: list[KnowledgeBase]) -> None:
        ReplayBufferManager.__init__(self, buffer_list)  # type: ignore
        self.traj_meta = {}  # {traj_id: [(start_idx, end_idx), (..., ...)]}

    def add(
        self,
        batch: KBBatchProtocol,
        buffer_ids: np.ndarray | list[int] | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Adds a batch of data into replay buffer."""
        # preprocess batch
        new_batch = Batch()
        for key in self._reserved_keys:
            new_batch.__dict__[key] = batch[key]
        batch = new_batch

        assert {"latent_obs", "act", "rew", "traj_id"}.issubset(
            batch.get_keys(),
        )

        # get index
        if buffer_ids is None:
            buffer_ids = np.arange(self.buffer_num)

        ptrs, ep_lens, ep_rews, ep_idxs = [], [], [], []
        for batch_idx, buffer_id in enumerate(buffer_ids):
            ptr, ep_rew, ep_len, ep_idx = self.buffers[buffer_id]._add_index(
                batch.rew[batch_idx],
                done=False,
            )
            ptrs.append(ptr + self._offset[buffer_id])
            ep_lens.append(ep_len)
            ep_rews.append(ep_rew.astype(np.float32))
            ep_idxs.append(ep_idx + self._offset[buffer_id])
            self.last_index[buffer_id] = ptr + self._offset[buffer_id]
            self._lengths[buffer_id] = len(self.buffers[buffer_id])

            traj_id = batch.traj_id[batch_idx]
            if traj_id not in self.traj_meta:
                # new trajectory, need to allocate space in traj_meta
                self.traj_meta[traj_id] = [None] * self.buffer_num

            if self.traj_meta[traj_id][buffer_id] is None:
                # first occurence of this trajectory in this buffer
                self.traj_meta[traj_id][buffer_id] = (
                    self.last_index[buffer_id],
                    self.last_index[buffer_id],
                )
            else:
                self.traj_meta[traj_id][buffer_id] = (
                    self.traj_meta[traj_id][buffer_id][0],
                    self.last_index[buffer_id],
                )
        ptrs = np.array(ptrs)

        try:
            self._meta[ptrs] = batch
        except ValueError:
            batch.rew = batch.rew.astype(np.float32)
            if len(self._meta.get_keys()) == 0:
                self._meta = create_value(batch, self.maxsize, stack=False)  # type: ignore
            else:  # dynamic key pops up in batch
                alloc_by_keys_diff(self._meta, batch, self.maxsize, False)
            self._set_batch_for_children()
            self._meta[ptrs] = batch

        return (
            ptrs,
            np.array(ep_rews),
            np.array(ep_lens),
            np.array(ep_idxs),
        )

    def get_trajectory(self, traj_id: int) -> List[Optional[KBBatchProtocol]]:
        """
        Retrieves the trajectory data for the given traj_id from each buffer.

        Returns a list where each element corresponds to the trajectory data
        from a specific buffer. If a buffer does not contain data for the
        traj_id, its corresponding element in the list will be None.
        """
        trajectory_data_per_buffer = []
        for buffer_id, buffer in enumerate(self.buffers):
            traj_segment = self.traj_meta[traj_id][buffer_id]
            if traj_segment is not None:
                start_idx, end_idx = traj_segment
                # adjust indices relative to the buffer
                buffer_start_idx = start_idx - self._offset[buffer_id]
                buffer_end_idx = (
                    end_idx - self._offset[buffer_id] + 1
                )  # Include end_idx

                segment_data = buffer[buffer_start_idx:buffer_end_idx]
                trajectory_data_per_buffer.append(segment_data)
            else:
                # no data for this traj_id in this buffer
                trajectory_data_per_buffer.append(None)

        return trajectory_data_per_buffer


class VectorKnowledgeBase(KnowledgeBaseManager):
    """A class containing `buffer_num` knowledge bases.

    Note that, conceptually speaking, the knowledge base is only one. This class is merely an implementation-level convenience, its point being to provide a frictionless interaction with Tianshou.
    """

    def __init__(self, total_size: int, buffer_num: int, **kwargs: Any) -> None:
        assert buffer_num > 0
        size = int(np.ceil(total_size / buffer_num))
        buffer_list = [KnowledgeBase(size, **kwargs) for _ in range(buffer_num)]
        super().__init__(buffer_list)
