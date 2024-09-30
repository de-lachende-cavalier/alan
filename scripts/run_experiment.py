import argparse
from datetime import datetime
import os

import torch
import gymnasium as gym
import tianshou as ts
from tianshou.utils import TensorboardLogger
from torch.utils.tensorboard import SummaryWriter
import numpy as np

from models import SelfModel, EnvModel
from intrinsic import ICM
from config import ConfigManager
from utils.experiment import ExperimentFactory

DEFAULT_DEVICE = "cpu"
ART_DIR = "artefacts"
LOG_DIR = f"{ART_DIR}/logs"
PLOT_DIR = f"{ART_DIR}/plots"
REC_DIR = f"{ART_DIR}/recs"


def setup_config(base_config_path, env_config, policy_config, obsnet_config):
    """Sets up and validate the configuration."""
    config = ConfigManager(base_config_path)
    config.create_config(
        {
            "environment": env_config,
            "policy": policy_config,
            "obsnet": obsnet_config,
        }
    )
    return config


def setup_environment(config):
    """Sets up the gym environment."""
    env_name = config.get("environment.name")
    try:
        # return an empty dictionary in case the env_config is empty/doesn't exist
        env = gym.make(env_name, **config.get("environment.env_config", {}))
        return env, env_name
    except gym.error.Error as e:
        raise RuntimeError(f"Failed to create environment {env_name}: {e}")


def setup_vector_envs(env, config):
    """Sets up vector environments for training and testing."""
    num_train_envs = config.get("environment.num_train_envs")
    num_test_envs = config.get("environment.num_test_envs")
    train_envs = ts.env.DummyVectorEnv([lambda: env for _ in range(num_train_envs)])
    test_envs = ts.env.DummyVectorEnv([lambda: env for _ in range(num_test_envs)])
    return train_envs, test_envs


def setup_buffers(config, num_train_envs, num_test_envs, factory):
    """Sets up replay buffers for training and testing."""
    train_buf_size = config.get("buffers.train_buf_size")
    test_buf_size = config.get("buffers.test_buf_size")
    train_buf = factory.create_buffer(train_buf_size, num_train_envs)
    test_buf = factory.create_buffer(test_buf_size, num_test_envs)
    return train_buf, test_buf


def setup_networks(factory, env, device):
    """Sets up observation, actor, and critic networks."""
    obs_net = factory.create_obsnet(env.observation_space)
    actor_net, critic_net = factory.create_actor_critic(
        obs_net, env.action_space, device
    )
    return obs_net, actor_net, critic_net


def setup_models(obs_net, env, train_buf, device):
    """Sets up environment and self models."""
    # TODO can't I use YAML for more flexibility here? re-think this when I introduce other intrinsic modules
    # intrinsic module and horizon can easily fit in a yaml config
    env_model = EnvModel()
    self_model = SelfModel(
        obs_net, env.action_space, train_buf, ICM, her_horizon=3, device=device
    )
    return env_model, self_model


def setup_policy(
    factory, self_model, env_model, actor_net, critic_net, optimizer, env, device
):
    """Sets up the policy."""
    return factory.create_policy(
        self_model,
        env_model,
        actor_net,
        critic_net,
        optimizer,
        env.action_space,
        env.observation_space,
        False,  # no action scaling
        device,
    )


def setup_collectors(factory, policy, train_envs, test_envs, train_buf, test_buf):
    """Sets up collectors for training and testing."""
    train_collector = factory.create_collector(policy, train_envs, train_buf)
    test_collector = factory.create_collector(policy, test_envs, test_buf)
    return train_collector, test_collector


def setup_logger(env_name, policy_name, obsnet_name, is_goal_aware):
    """Sets up the TensorboardLogger."""
    log_path = _make_save_path(
        LOG_DIR, env_name, policy_name, obsnet_name, is_goal_aware
    )
    writer = SummaryWriter(log_path)
    return TensorboardLogger(writer)


def plot(
    factory,
    epoch_stats,
    env_name,
    policy_name,
    obsnet_name,
    is_goal_aware,
    save_pdf=True,
):
    """Plots the data.

    Its default behaviour is to save the plot to a PDF file to not block execution."""
    plot_path = _make_save_path(
        PLOT_DIR, env_name, policy_name, obsnet_name, is_goal_aware
    )

    plotter = factory.create_plotter(epoch_stats)
    # if save_pdf is False the path will be ignored
    plotter.plot(figsize=(12, 8), save_pdf=save_pdf, pdf_path=plot_path)


def record_rollout(env, policy):
    """Records a rollout lasting one episode."""
    obs, info = env.reset()
    done = False

    while not done:
        action = policy(ts.data.Batch(obs=np.array([obs]), info=np.array([info]))).act
        obs, info, terminated, truncated, _ = env.step(action)
        done = terminated or truncated

    env.close()


def run_experiment(trainer):
    """Runs the experiment and collect epoch statistics."""
    epoch_stats = []
    for epoch_stat in trainer:
        epoch_stats.append(epoch_stat)
    return epoch_stats


def _make_save_path(
    base_path, env_name, policy_name, obsnet_name, is_goal_aware, ext=None
):
    """Creates a path to save the artefacts (plots, recordings and logs) to.

    It returns the path (after creating it on the filesystem)."""
    timestamp = datetime.now().strftime("%d%m%Y-%H%M%S")
    save_path = os.path.join(
        base_path,
        env_name.lower(),
        policy_name,
        obsnet_name,
        "goal" if is_goal_aware else "vanilla",
        timestamp if not ext else f"{timestamp}.{ext}",
    )
    # make the path as well (and leave things unchanged if it already exists)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    return save_path


def main(
    base_config_path: str,
    env_config: str,
    policy_config: str,
    obsnet_config: str,
    device: torch.device,
) -> None:
    config = setup_config(base_config_path, env_config, policy_config, obsnet_config)
    factory = ExperimentFactory(config)

    print("[+] Setting up the environment...")
    env, env_name = setup_environment(config)
    rec_path = _make_save_path(
        REC_DIR,
        env_name.lower(),
        policy_config,
        obsnet_config,
        factory.is_goal_aware,
        ext="ttyrec",
    )
    env = factory.wrap_env(env, rec_path)

    print("[+] Setting up the buffers...")
    train_envs, test_envs = setup_vector_envs(env, config)
    train_buf, test_buf = setup_buffers(
        config, len(train_envs), len(test_envs), factory
    )

    print("[+] Setting up the networks...")
    obs_net, actor_net, critic_net = setup_networks(factory, env, device)
    env_model, self_model = setup_models(obs_net, env, train_buf, device)

    print("[+] Setting up the policy...")
    # TODO this read might not be a good idea, performance-wise (should probably do something like what I did for env_name)
    lr = config.get("policy.learning_rate")
    combined_params = set(list(actor_net.parameters()) + list(critic_net.parameters()))
    optimizer = torch.optim.Adam(combined_params, lr=lr)

    policy = setup_policy(
        factory, self_model, env_model, actor_net, critic_net, optimizer, env, device
    )

    print("[+] Setting up the collector...")
    train_collector, test_collector = setup_collectors(
        factory, policy, train_envs, test_envs, train_buf, test_buf
    )

    print("[+] Setting up the trainer...")
    logger = setup_logger(env_name, policy_config, obsnet_config, factory.is_goal_aware)

    trainer = factory.create_trainer(
        policy, train_collector, test_collector, logger, device
    )

    print("[+] Running the experiment...")
    epoch_stats = run_experiment(trainer)

    save_pdf = True
    print("[+] Plotting..." if not save_pdf else "[+] Saving the plot...")
    plot(
        factory,
        epoch_stats,
        env_name,
        policy_config,
        obsnet_config,
        factory.is_goal_aware,
        save_pdf=save_pdf,
    )

    print("[+] Recording a rollout...")
    record_rollout(env, policy)

    print("[+] All done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="""
            The Reinforcement Learning Experiment Runner.

            This script sets up and runs reinforcement learning experiments with customizable environments, policies, and observation networks.

            Experiments are defined through YAML configuration files (usually located in the config/ directory), allowing for easy parameter tuning and reproducibility.
            """
    )

    # the config needs to be passed as a full path because we need to extract the path to the config dir from it (that is what allows us to pass the rest of the arguments by name and not path)
    parser.add_argument(
        "-c",
        "--config",
        type=str,
        required=True,
        help="Path to the base configuration file (including .yaml extension)",
        metavar="BASE_CONFIG_PATH",
    )
    parser.add_argument(
        "-e",
        "--env",
        type=str,
        required=True,
        help="Name of the environment config file (without .yaml extension)",
        metavar="ENV_CONFIG",
    )
    parser.add_argument(
        "-p",
        "--policy",
        type=str,
        required=True,
        help="Name of the policy config file (without .yaml extension)",
        metavar="POLICY_CONFIG",
    )
    parser.add_argument(
        "-o",
        "--obsnet",
        type=str,
        required=True,
        help="Name of the observation network config file (without .yaml extension)",
        metavar="OBSNET_CONFIG",
    )
    parser.add_argument(
        "-d",
        "--device",
        type=str,
        default=DEFAULT_DEVICE,
        choices=["cpu", "cuda", "mps"],
        help="Device to run on",
    )

    args = parser.parse_args()

    device = torch.device(args.device)
    main(args.config, args.env, args.policy, args.obsnet, device=device)