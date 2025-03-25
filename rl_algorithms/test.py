import time
import torch
import math
from tqdm import tqdm
from tensordict import TensorDict
from rl_algorithms.utils import make_env
from rl_algorithms.train import get_performance_metrics
from rl_algorithms.utils import set_unique_seed

# Functions
def get_z_score_torch(confidence_level):
    """
    Compute the z-score for a given confidence level using PyTorch.
    Args:
        confidence_level (float): Confidence level (e.g., 0.95 for 95% confidence).
    Returns:
        float: The z-score corresponding to the confidence level.
    """
    alpha = 1 - confidence_level
    return torch.distributions.Normal(0, 1).icdf(torch.tensor(1 - alpha / 2)).item()

def compute_summary_stats(metrics, confidence_level=0.95):
    """
    Compute mean, median, std, min, and max for each metric in the dictionary.
    Args:
        metrics (dict): Dictionary with metrics as tensors.
    Returns:
        dict: Summary statistics for each metric.
    """
    summary_stats = {}
    z = get_z_score_torch(confidence_level)
    for key, values in metrics.items():
        n = values.numel()  # Number of elements in the tensor
        mean = values.mean().item()
        std = values.std().item()
        margin_of_error = z * (std / math.sqrt(n)) if n > 1 else 0.0  # CI margin of error

        summary_stats[key] = {
            "mean": mean,
            "median": values.median().item(),
            "std": std,
            "min": values.min().item(),
            "max": values.max().item(),
            "lb_ci": mean - margin_of_error,  # Lower bound of the CI
            "ub_ci": mean + margin_of_error,  # Upper bound of the CI
        }
    return summary_stats

# Main function
def evaluate_model(policy, config, device=torch.device("cuda"), **kwargs):
    """
    Evaluate the policy and critic on the test environment.

    Args:
        policy (ProbabilisticActor): The policy to evaluate.
        env_kwargs (dict): Environment configuration.
        device (torch.device): Device to perform computations.
        num_episodes (int): Number of episodes for evaluation.
        n_step (int): Maximum steps per episode.

    Returns:
        dict: A dictionary of evaluation metrics.
    """
    # Extract evaluation hyperparameters
    env_kwargs = config.env
    num_episodes = kwargs.get("num_episodes", 10)

    # Create the test environment # todo: would using multiple paths as scenario tree change results?
    max_paths = 2 # Run small batch, as we care about instances
    test_env = make_env(env_kwargs, batch_size=[max_paths], device=device)
    n_step = test_env.T * test_env.K  # Maximum steps per episode (T x K)
    feas_threshold = 1.0
    delta = 1.0

    # Set policy to evaluation mode
    policy.eval()  # Set policy to evaluation mode

    # Initialize metrics storage
    metrics = {
        "total_profit": torch.zeros(num_episodes, device=device),  # [num_episodes]
        "total_violations": torch.zeros(num_episodes, device=device),  # [num_episodes]
        "inference_times": torch.zeros(num_episodes, device=device),  # [num_episodes]
        "feasible_instance":torch.zeros(num_episodes, device=device),  # [num_episodes]
        "demand_violations": torch.zeros(num_episodes, device=device),  # [num_episodes]
        "capacity_violations": torch.zeros(num_episodes, device=device),  # [num_episodes]
        "stability_violations": torch.zeros(num_episodes, device=device),  # [num_episodes]
        "pbs_violations": torch.zeros(num_episodes, device=device),  # [num_episodes]
    }

    with torch.no_grad():
        # Warm-up phase
        for _ in tqdm(range(3), desc="Warm-up"):
            _ = test_env.rollout(
                policy=policy,
                max_steps=n_step,
                auto_reset=True,
            )

        for episode in tqdm(range(num_episodes), desc="Episodes"):
            # Update seeds
            seed = config.env.seed + episode + 1
            set_unique_seed(seed)
            config.env.seed = seed

            # Setup new environment on cpu (same instance as scenario tree)
            gen_env = make_env(config.env, batch_size=[max_paths], device='cpu')
            td = gen_env.reset().to(device)

            # Synchronize and measure inference time
            torch.cuda.synchronize() if device.type == "cuda" else None
            start_time = time.perf_counter()

            # # Perform rollout
            trajectory = test_env.rollout(
                policy=policy,
                max_steps=n_step,
                auto_reset=False,
                tensordict=td,
            )

            # Synchronize and measure inference time
            torch.cuda.synchronize() if device.type == "cuda" else None
            end_time = time.perf_counter()

            # Deal with inaccurate computations; violations should be 0 if action is 0 and clip_max is 0
            zero_idx = torch.where((trajectory["action"][0] == 0.0) & (trajectory["clip_max"][0] == 0.0))
            trajectory["violation"][0][zero_idx] = 0.0

            # Extract episode-level metrics - use batch = 0 for ground truth instance
            metrics["total_profit"][episode] = trajectory["profit"][0].sum()
            metrics["total_violations"][episode] = trajectory["violation"][0].sum()
            metrics["inference_times"][episode] = end_time - start_time

            # Determine feasibility per instance based on adjusted violations; violations < delta are set to 0
            violation_adjusted = trajectory["violation"][0].clone()
            v_mask = violation_adjusted[:, :-4] < delta
            violation_adjusted[:, :-4][v_mask] = 0.0 # Do not apply this to stability constraints
            violation_adjusted[:, -4:][~test_env.next_port_mask] = 0.0  # Only care about feasibility between ports
            metrics["feasible_instance"][episode] = 1.0 if violation_adjusted.sum() <= feas_threshold else 0.0
            # todo: analyze this; check if it is correct
            metrics["demand_violations"][episode] = violation_adjusted[:, 0].sum()
            metrics["capacity_violations"][episode] = violation_adjusted[:, 1:-4].sum()
            metrics["stability_violations"][episode] = violation_adjusted[:, -4:].sum()
            metrics["pbs_violations"][episode] = trajectory["observation", "excess_pod_locations"][0].sum()

            # Close the generated environment
            gen_env.close()
            del gen_env  # Explicitly delete to free memory

    # Summarize episode-level metrics (mean and std)
    summary_stats = compute_summary_stats(metrics)
    test_env.close()
    return metrics, summary_stats
