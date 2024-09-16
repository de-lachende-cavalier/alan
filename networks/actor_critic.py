from typing import Dict, Optional, Self
from tianshou.data.types import ObsBatchProtocol
from core.types import GoalBatchProtocol, ObservationNetProtocol
import gymnasium as gym
from torch import nn
import torch


class SimpleNetHackActor(nn.Module):
    def __init__(
        self,
        obs_net: ObservationNetProtocol,
        action_space: gym.Space,
        device: torch.device = torch.device("cpu"),
    ):
        super().__init__()
        self.device = device
        self.obs_net = obs_net.to(device)
        self.n_actions = action_space.n
        self.final_layer = nn.Linear(self.obs_net.o_dim, self.n_actions).to(device)

    def forward(
        self,
        batch_obs: ObsBatchProtocol,
        state: Optional[torch.Tensor] = None,
        info: Dict = {},
    ):
        obs_out = self.obs_net(batch_obs)
        logits = self.final_layer(obs_out)
        return logits, state

    def to(self, device: torch.device) -> Self:
        self.device = device
        self.obs_net = self.obs_net.to(device)
        self.final_layer = self.final_layer.to(device)
        return super().to(device)


class GoalNetHackActor(SimpleNetHackActor):
    def __init__(
        self,
        obs_net: ObservationNetProtocol,
        action_space: gym.Space,
        device: torch.device = torch.device("cpu"),
    ):
        super().__init__(obs_net, action_space, device)
        hidden_dim = obs_net.o_dim // 3
        self.obs_munet = nn.Sequential(
            nn.Linear(obs_net.o_dim, hidden_dim), nn.ReLU()
        ).to(device)
        self.goal_munet = nn.Sequential(
            nn.Linear(obs_net.o_dim, hidden_dim), nn.ReLU()
        ).to(device)
        self.final_layer = nn.Linear(hidden_dim + hidden_dim, self.n_actions).to(device)

    def forward(
        self,
        batch_obs_goal: GoalBatchProtocol,
        state: Optional[torch.Tensor] = None,
        info: Dict = {},
    ):
        batch_obs = {k: v for k, v in batch_obs_goal.items() if k != "latent_goal"}
        obs_out = self.obs_net(batch_obs)
        obss = self.obs_munet(obs_out)
        goals = self.goal_munet(
            torch.as_tensor(
                batch_obs_goal["latent_goal"], dtype=torch.float32, device=self.device
            )
        )
        logits = self.final_layer(torch.cat((obss, goals), dim=1))
        return logits, state

    def to(self, device: torch.device) -> Self:
        super().to(device)
        self.obs_munet = self.obs_munet.to(device)
        self.goal_munet = self.goal_munet.to(device)
        return self


class SimpleNetHackCritic(nn.Module):
    def __init__(
        self,
        obs_net: ObservationNetProtocol,
        device: torch.device = torch.device("cpu"),
    ):
        super().__init__()
        self.device = device
        self.obs_net = obs_net.to(device)
        self.final_layer = nn.Linear(self.obs_net.o_dim, 1).to(device)

    def forward(
        self,
        batch_obs: ObsBatchProtocol,
        state: Optional[torch.Tensor] = None,
        info: Dict = {},
    ):
        obs_out = self.obs_net(batch_obs)
        v_s = self.final_layer(obs_out)
        return v_s

    def to(self, device: torch.device) -> Self:
        self.device = device
        self.obs_net = self.obs_net.to(device)
        self.final_layer = self.final_layer.to(device)
        return super().to(device)


class GoalNetHackCritic(SimpleNetHackCritic):
    def __init__(
        self,
        obs_net: ObservationNetProtocol,
        device: torch.device = torch.device("cpu"),
    ):
        super().__init__(obs_net, device)
        hidden_dim = obs_net.o_dim // 3
        self.obs_munet = nn.Sequential(
            nn.Linear(obs_net.o_dim, hidden_dim), nn.ReLU()
        ).to(device)
        self.goal_munet = nn.Sequential(
            nn.Linear(obs_net.o_dim, hidden_dim), nn.ReLU()
        ).to(device)
        self.final_layer = nn.Linear(hidden_dim + hidden_dim, 1).to(device)

    def forward(
        self,
        batch_obs_goal: GoalBatchProtocol,
        state: Optional[torch.Tensor] = None,
        info: Dict = {},
    ):
        batch_obs = {k: v for k, v in batch_obs_goal.items() if k != "latent_goal"}
        obs_out = self.obs_net(batch_obs)
        obss = self.obs_munet(obs_out)
        goals = self.goal_munet(
            torch.as_tensor(
                batch_obs_goal["latent_goal"], dtype=torch.float32, device=self.device
            )
        )
        v_s = self.final_layer(torch.cat((obss, goals), dim=1))
        return v_s

    def to(self, device: torch.device) -> Self:
        super().to(device)
        self.obs_munet = self.obs_munet.to(device)
        self.goal_munet = self.goal_munet.to(device)
        return self
