import time
from copy import copy
from typing import Any, cast, TypeVar

import gymnasium as gym
import numpy as np
import torch
from overrides import override

from tianshou.data import (
    Batch,
    ReplayBuffer,
    to_numpy,
    Collector,
    CollectStats,
    AsyncCollector,
)
from tianshou.data.types import (
    ObsBatchProtocol,
    RolloutBatchProtocol,
)
from tianshou.env import BaseVectorEnv
from tianshou.utils.torch_utils import torch_train_mode

from policies import CorePolicy


class GoalBasedCollector(Collector):
    def __init__(
        self,
        policy: CorePolicy,
        env: gym.Env | BaseVectorEnv,
        buffer: ReplayBuffer | None = None,
        exploration_noise: bool = False,
    ) -> None:
        super().__init__(policy, env, buffer, exploration_noise=exploration_noise)

    def _compute_action_policy_hidden(
        self,
        random: bool,
        ready_env_ids_R: np.ndarray,
        last_obs_RO: np.ndarray,
        last_info_R: np.ndarray,
        last_hidden_state_RH: np.ndarray | torch.Tensor | Batch | None = None,
    ) -> tuple[np.ndarray, np.ndarray, Batch, np.ndarray | torch.Tensor | Batch | None]:
        """Returns the action, the normalized action, a "policy" entry, and the hidden state."""
        if random:
            try:
                act_normalized_RA = np.array(
                    [self._action_space[i].sample() for i in ready_env_ids_R],
                )
            # TODO: test whether envpool env explicitly
            except TypeError:  # envpool's action space is not for per-env
                act_normalized_RA = np.array(
                    [self._action_space.sample() for _ in ready_env_ids_R]
                )
            act_RA = self.policy.map_action_inverse(np.array(act_normalized_RA))
            policy_R = Batch()
            hidden_state_RH = None

        else:
            info_batch = _HACKY_create_info_batch(last_info_R)
            obs_batch_R = cast(
                ObsBatchProtocol, Batch(obs=last_obs_RO, info=info_batch)
            )

            act_batch_RA = self.policy(
                obs_batch_R,
                last_hidden_state_RH,
            )

            act_RA = to_numpy(act_batch_RA.act)
            if self.exploration_noise:
                act_RA = self.policy.exploration_noise(act_RA, obs_batch_R)
            act_normalized_RA = self.policy.map_action(act_RA)

            # TODO: cleanup the whole policy in batch thing
            policy_R = act_batch_RA.get("policy", Batch())
            if not isinstance(policy_R, Batch):
                raise RuntimeError(
                    f"The policy result should be a {Batch}, but got {type(policy_R)}",
                )

            hidden_state_RH = act_batch_RA.get("state", None)
            if hidden_state_RH is not None:
                policy_R.hidden_state = (
                    hidden_state_RH  # save state into buffer through policy attr
                )

            latent_obs_R = act_batch_RA.get("latent_obs", None)
            if latent_obs_R is None:
                raise RuntimeError("The latent observations should not be None!")

            latent_goal_R = act_batch_RA.get("latent_goal", None)
            if latent_goal_R is None:
                raise RuntimeError("The latent goals should not be None!")

        return (
            act_RA,
            act_normalized_RA,
            policy_R,
            hidden_state_RH,
            latent_obs_R,
            latent_goal_R,
        )

    # TODO: reduce complexity, remove the noqa
    def _collect(
        self,
        n_step: int | None = None,
        n_episode: int | None = None,
        random: bool = False,
        render: float | None = None,
        gym_reset_kwargs: dict[str, Any] | None = None,
    ) -> CollectStats:
        # TODO: can't do it init since AsyncCollector is currently a subclass of Collector
        if self.env.is_async:
            raise ValueError(
                f"Please use {AsyncCollector.__name__} for asynchronous environments. "
                f"Env class: {self.env.__class__.__name__}.",
            )

        if n_step is not None:
            ready_env_ids_R = np.arange(self.env_num)
        elif n_episode is not None:
            ready_env_ids_R = np.arange(min(self.env_num, n_episode))
        else:
            raise ValueError("Either n_step or n_episode should be set.")

        start_time = time.time()
        if self._pre_collect_obs_RO is None or self._pre_collect_info_R is None:
            raise ValueError(
                "Initial obs and info should not be None. "
                "Either reset the collector (using reset or reset_env) or pass reset_before_collect=True to collect.",
            )

        # get the first obs to be the current obs in the n_step case as
        # episodes as a new call to collect does not restart trajectories
        # (which we also really don't want)
        step_count = 0
        num_collected_episodes = 0
        episode_returns: list[float] = []
        episode_lens: list[int] = []
        episode_start_indices: list[int] = []

        # in case we select fewer episodes than envs, we run only some of them
        last_obs_RO = _nullable_slice(self._pre_collect_obs_RO, ready_env_ids_R)
        last_info_R = _nullable_slice(self._pre_collect_info_R, ready_env_ids_R)
        last_hidden_state_RH = _nullable_slice(
            self._pre_collect_hidden_state_RH,
            ready_env_ids_R,
        )

        while True:
            # todo check if we need this when using cur_rollout_batch
            # if len(cur_rollout_batch) != len(ready_env_ids):
            #     raise RuntimeError(
            #         f"The length of the collected_rollout_batch {len(cur_rollout_batch)}) is not equal to the length of ready_env_ids"
            #         f"{len(ready_env_ids)}. This should not happen and could be a bug!",
            #     )
            # restore the state: if the last state is None, it won't store

            # get the next action
            (
                act_RA,
                act_normalized_RA,
                policy_R,
                hidden_state_RH,
                latent_obs_R,
                latent_goal_R,
            ) = self._compute_action_policy_hidden(
                random=random,
                ready_env_ids_R=ready_env_ids_R,
                last_obs_RO=last_obs_RO,
                last_info_R=last_info_R,
                last_hidden_state_RH=last_hidden_state_RH,
            )

            obs_next_RO, rew_R, terminated_R, truncated_R, info_R = self.env.step(
                act_normalized_RA,
                ready_env_ids_R,
            )
            if isinstance(info_R, dict):  # type: ignore[unreachable]
                # This can happen if the env is an envpool env. Then the info returned by step is a dict
                info_R = _dict_of_arr_to_arr_of_dicts(info_R)  # type: ignore[unreachable]
            done_R = np.logical_or(terminated_R, truncated_R)

            current_iteration_batch = cast(
                RolloutBatchProtocol,
                Batch(
                    obs=last_obs_RO,
                    act=act_RA,
                    policy=policy_R,
                    obs_next=obs_next_RO,
                    latent_obs=latent_obs_R,
                    latent_goal=latent_goal_R,
                    rew=rew_R,
                    terminated=terminated_R,
                    truncated=truncated_R,
                    done=done_R,
                    info=info_R,
                ),
            )

            # TODO: only makes sense if render_mode is human.
            #  Also, doubtful whether it makes sense at all for true vectorized envs
            if render:
                self.env.render()
                if not np.isclose(render, 0):
                    time.sleep(render)

            # add data into the buffer
            ptr_R, ep_rew_R, ep_len_R, ep_idx_R = self.buffer.add(
                current_iteration_batch,
                buffer_ids=ready_env_ids_R,
            )

            # collect statistics
            num_episodes_done_this_iter = np.sum(done_R)
            num_collected_episodes += num_episodes_done_this_iter
            step_count += len(ready_env_ids_R)

            # preparing for the next iteration
            # obs_next, info and hidden_state will be modified inplace in the code below,
            # so we copy to not affect the data in the buffer
            last_obs_RO = copy(obs_next_RO)
            last_info_R = copy(info_R)
            last_hidden_state_RH = copy(hidden_state_RH)

            # Preparing last_obs_RO, last_info_R, last_hidden_state_RH for the next while-loop iteration
            # Resetting envs that reached done, or removing some of them from the collection if needed (see below)
            if num_episodes_done_this_iter > 0:
                # TODO: adjust the whole index story, don't use np.where, just slice with boolean arrays
                # D - number of envs that reached done in the rollout above
                env_ind_local_D = np.where(done_R)[0]
                env_ind_global_D = ready_env_ids_R[env_ind_local_D]
                episode_lens.extend(ep_len_R[env_ind_local_D])
                episode_returns.extend(ep_rew_R[env_ind_local_D])
                episode_start_indices.extend(ep_idx_R[env_ind_local_D])
                # now we copy obs_next to obs, but since there might be
                # finished episodes, we have to reset finished envs first.

                gym_reset_kwargs = gym_reset_kwargs or {}
                obs_reset_DO, info_reset_D = self.env.reset(
                    env_id=env_ind_global_D,
                    **gym_reset_kwargs,
                )

                # Set the hidden state to zero or None for the envs that reached done
                # TODO: does it have to be so complicated? We should have a single clear type for hidden_state instead of
                #  this complex logic
                self._reset_hidden_state_based_on_type(
                    env_ind_local_D, last_hidden_state_RH
                )

                # preparing for the next iteration
                last_obs_RO[env_ind_local_D] = obs_reset_DO
                last_info_R[env_ind_local_D] = info_reset_D

                # Handling the case when we have more ready envs than desired and are not done yet
                #
                # This can only happen if we are collecting a fixed number of episodes
                # If we have more ready envs than there are remaining episodes to collect,
                # we will remove some of them for the next rollout
                # One effect of this is the following: only envs that have completed an episode
                # in the last step can ever be removed from the ready envs.
                # Thus, this guarantees that each env will contribute at least one episode to the
                # collected data (the buffer). This effect was previous called "avoiding bias in selecting environments"
                # However, it is not at all clear whether this is actually useful or necessary.
                # Additional naming convention:
                # S - number of surplus envs
                # TODO: can the whole block be removed? If we have too many episodes, we could just strip the last ones.
                #   Changing R to R-S highly increases the complexity of the code.
                if n_episode:
                    remaining_episodes_to_collect = n_episode - num_collected_episodes
                    surplus_env_num = (
                        len(ready_env_ids_R) - remaining_episodes_to_collect
                    )
                    if surplus_env_num > 0:
                        # R becomes R-S here, preparing for the next iteration in while loop
                        # Everything that was of length R needs to be filtered and become of length R-S.
                        # Note that this won't be the last iteration, as one iteration equals one
                        # step and we still need to collect the remaining episodes to reach the breaking condition.

                        # creating the mask
                        env_to_be_ignored_ind_local_S = env_ind_local_D[
                            :surplus_env_num
                        ]
                        env_should_remain_R = np.ones_like(ready_env_ids_R, dtype=bool)
                        env_should_remain_R[env_to_be_ignored_ind_local_S] = False
                        # stripping the "idle" indices, shortening the relevant quantities from R to R-S
                        ready_env_ids_R = ready_env_ids_R[env_should_remain_R]
                        last_obs_RO = last_obs_RO[env_should_remain_R]
                        last_info_R = last_info_R[env_should_remain_R]
                        if hidden_state_RH is not None:
                            last_hidden_state_RH = last_hidden_state_RH[env_should_remain_R]  # type: ignore[index]

            if (n_step and step_count >= n_step) or (
                n_episode and num_collected_episodes >= n_episode
            ):
                break

        # generate statistics
        self.collect_step += step_count
        self.collect_episode += num_collected_episodes
        collect_time = max(time.time() - start_time, 1e-9)
        self.collect_time += collect_time

        if n_step:
            # persist for future collect iterations
            self._pre_collect_obs_RO = last_obs_RO
            self._pre_collect_info_R = last_info_R
            self._pre_collect_hidden_state_RH = last_hidden_state_RH
        elif n_episode:
            # reset envs and the _pre_collect fields
            self.reset_env(gym_reset_kwargs)  # todo still necessary?

        return CollectStats.with_autogenerated_stats(
            returns=np.array(episode_returns),
            lens=np.array(episode_lens),
            n_collected_episodes=num_collected_episodes,
            n_collected_steps=step_count,
            collect_time=collect_time,
            collect_speed=step_count / collect_time,
        )


def _HACKY_create_info_batch(info_array: np.ndarray) -> Batch:
    """TODO: this exists because of multiple bugs in Batch and to restore backwards compatibility.
    Batch should be fixed and this function should be removed asap!.
    """
    if info_array.dtype != np.dtype("O"):
        raise ValueError(
            f"Expected info_array to have dtype=object, but got {info_array.dtype}.",
        )

    truthy_info_indices = info_array.nonzero()[0]
    falsy_info_indices = set(range(len(info_array))) - set(truthy_info_indices)
    falsy_info_indices = np.array(list(falsy_info_indices), dtype=int)

    if len(falsy_info_indices) == len(info_array):
        return Batch()

    some_nonempty_info = None
    for info in info_array:
        if info:
            some_nonempty_info = info
            break

    info_array = copy(info_array)
    info_array[falsy_info_indices] = some_nonempty_info
    result_batch_parent = Batch(info=info_array)
    result_batch_parent.info[falsy_info_indices] = {}
    return result_batch_parent.info


_TArrLike = TypeVar("_TArrLike", bound="np.ndarray | torch.Tensor | Batch | None")


def _nullable_slice(obj: _TArrLike, indices: np.ndarray) -> _TArrLike:
    """Return None, or the values at the given indices if the object is not None."""
    if obj is not None:
        return obj[indices]  # type: ignore[index, return-value]
    return None  # type: ignore[unreachable]
