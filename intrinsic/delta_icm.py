from typing import Sequence
from core.types import LatentObsActNextBatchProtocol

import torch
import numpy as np

from .icm import ICM


class DeltaICM(ICM):
    """An implementation of ICM that applies a form of delta encoding (https://en.wikipedia.org/wiki/Delta_encoding) to the intrinsic reward.

    We employ delta encoding because in difficult environments with a great deal of novelty (e.g., NetHack), the agent gets flooded with fast intrinsic rewards, ignoring the extrinsic ones.
    This implementation aims to ameliorate this issue by only rewarding the agent for differences in intrinsic rewards, attempting to mimic neural adaptation (https://en.wikipedia.org/wiki/Neural_adaptation).
    """

    def __init__(
        self,
        feature_dim: int,
        n_actions: int,
        batch_size: int,
        learning_rate: float = 1e-3,
        hidden_sizes: Sequence[int] = [256, 128, 64],
        beta: float = 0.2,
        eta: float = 0.07,
        device: torch.device = torch.device("cpu"),
    ) -> None:
        super().__init__(
            feature_dim=feature_dim,
            n_actions=n_actions,
            batch_size=batch_size,
            learning_rate=learning_rate,
            hidden_sizes=hidden_sizes,
            beta=beta,
            eta=eta,
            device=device,
        )

        self.running_avg_intrinsic = 0.0
        self.n_intrinsic = 0

    def get_reward(self, batch: LatentObsActNextBatchProtocol) -> np.ndarray:
        vanilla_intrew = super().get_reward(batch)
        self.n_intrinsic += vanilla_intrew.size

        # vanilla_intrew is a vector of shape (num_envs,)
        self.running_avg_intrinsic += np.mean(
            vanilla_intrew - self.running_avg_intrinsic
        ) / (self.n_intrinsic + 1)

        # let the runninng average increase in importance as we get more samples
        alpha = self._normalised_log(self.n_intrinsic)
        # using abs() here provides an interesting side effect: when the intrinsic reward diminishes, the delta will be kept higher than usual due to the large running average
        delta_int_rew = np.abs(vanilla_intrew - alpha * self.running_avg_intrinsic)

        # clip reward to be in [0, 1] range due to HER
        intrinsic_reward = np.clip(delta_int_rew * self.eta, 0.0, 1.0, dtype=np.float32)
        return intrinsic_reward

    def _normalised_log(self, n: int, max_n: int = 10_000) -> np.float32:
        """Computes a normalised log (with base e), i.e., a log that returns values in the range [0, 1]."""
        # TODO max_n is rather arbitrary...
        if n >= max_n:
            # return 1 if we collected at least max_n intrinsic rewards
            return 1

        max_log = np.log(max_n, dtype=np.float32)
        # min-max normalisation (with the min_log == 0)
        return np.log(n, dtype=np.float32) / max_log
