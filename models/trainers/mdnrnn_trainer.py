from typing import Dict, Tuple

from tianshou.data import SequenceSummaryStats, Batch, ReplayBuffer

import torch
from torch import nn
import torch.nn.functional as F
from torch.optim.lr_scheduler import ReduceLROnPlateau
import numpy as np

from models.utils import gmm_loss


class MDNRNNTrainer:
    """Trainer class for the MDNRNN model.

    Adapted from https://github.com/ctallec/world-models/blob/master/trainmdrnn.py."""

    def __init__(
        self,
        mdnrnn: nn.Module,
        vae: nn.Module,
        batch_size: int,
        learning_rate: float = 1e-3,
        alpha: float = 0.9,
        device: torch.device = torch.device("cpu"),
    ):
        self.mdnrnn = mdnrnn.to(device)
        self.vae = vae.to(device)

        self.optimizer = torch.optim.RMSprop(
            self.mdnrnn.parameters(), lr=learning_rate, alpha=alpha
        )
        self.scheduler = ReduceLROnPlateau(
            self.optimizer, "min", factor=0.5, patience=5
        )

        self.batch_size = batch_size
        self.device = device

    def train(self, data: Batch | ReplayBuffer) -> Tuple[
        SequenceSummaryStats,
        SequenceSummaryStats,
        SequenceSummaryStats,
        SequenceSummaryStats,
    ]:
        """Trains the MDNRNN model for one epoch."""
        # train for one epoch only because we expect to accumulate plenty of data over the agent's lifetime
        losses_summary, gmm_losses_summary, bce_losses_summary, mse_losses_summary = (
            self._data_pass(data)
        )
        self.scheduler.step(losses_summary.mean)
        return (
            losses_summary,
            gmm_losses_summary,
            bce_losses_summary,
            mse_losses_summary,
        )

    def _data_pass(
        self,
        data: Batch | ReplayBuffer,
    ) -> Tuple[
        SequenceSummaryStats,
        SequenceSummaryStats,
        SequenceSummaryStats,
        SequenceSummaryStats,
    ]:
        """Performs one pass through the data."""
        losses, gmm_losses, bce_losses, mse_losses = [], [], [], []
        for batch in data.split(self.batch_size, merge_last=True):
            latent_obs, *_ = self.vae.encoder(batch.obs)
            latent_obs_next, *_ = self.vae.encoder(batch.obs_next)

            self.optimizer.zero_grad()
            loss_dict = self._get_loss(
                latent_obs, batch.act, batch.rew, batch.done, latent_obs_next
            )
            loss_dict["loss"].backward()
            self.optimizer.step()

            losses.append(loss_dict["loss"].item())
            gmm_losses.append(loss_dict["gmm"].item())
            bce_losses.append(loss_dict["bce"].item())
            mse_losses.append(loss_dict["mse"].item())

        losses_summary = SequenceSummaryStats.from_sequence(losses)
        gmm_losses_summary = SequenceSummaryStats.from_sequence(gmm_losses)
        bce_losses_summary = SequenceSummaryStats.from_sequence(bce_losses)
        mse_losses_summary = SequenceSummaryStats.from_sequence(mse_losses)
        return (
            losses_summary,
            gmm_losses_summary,
            bce_losses_summary,
            mse_losses_summary,
        )

    def _get_loss(
        self,
        latent_obs: torch.Tensor,
        action: torch.Tensor,
        reward: np.ndarray,
        terminal: np.ndarray,
        latent_obs_next: torch.Tensor,
    ) -> Dict[str, torch.Tensor]:
        """Computes the losses for the MDNRNN model."""
        action = action.unsqueeze(1)
        terminal = torch.as_tensor(terminal, device=self.device).float()
        reward = torch.as_tensor(reward, device=self.device)

        mus, sigmas, logpi, rs, ds, _ = self.mdnrnn(action, latent_obs)

        gmm = gmm_loss(latent_obs_next, mus, sigmas, logpi)
        bce = F.binary_cross_entropy_with_logits(ds, terminal)

        latent_size = latent_obs.shape[1]
        mse = F.mse_loss(rs, reward)
        scale = latent_size + 2

        loss = (gmm + bce + mse) / scale
        return {"gmm": gmm, "bce": bce, "mse": mse, "loss": loss}
