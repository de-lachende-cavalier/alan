from typing import Tuple

import torch
import torch.nn.functional as F

from .vae_trainer import VAETrainer


class DiscreteVAETrainer(VAETrainer):
    """Trainer class for the Discrete VAE model."""

    def _get_loss(
        self,
        inputs,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Computes the VAE loss components."""
        reconstructions, z, dist = self.vae(inputs)

        reconstructions = torch.argmax(F.softmax(reconstructions, dim=-1), dim=-1)
        obs = torch.as_tensor(inputs.obs, device=self.vae.device, dtype=torch.float32)

        recon_loss = F.mse_loss(reconstructions, obs)
        kl_loss = self._compute_kl_loss(dist, z)
        total_loss = recon_loss + self.kl_weight * kl_loss
        return total_loss, recon_loss, kl_loss