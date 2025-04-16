import time
from typing import Optional, Iterable, List, Tuple, Dict, Union

import torch
import torch as th
from torch import Tensor
from tensordict.tensordict import TensorDict
from torchrl.envs.common import EnvBase
from torchrl.data import (
    Bounded,
    Unbounded,
    Composite,
)

# Modules
from environment.generator import MPP_Generator, UniformMPP_Generator
from environment.utils import *

class MasterPlanningEnv(EnvBase):
    """Reinforcement learning environment for solving the Master Planning Problem (MPP) in container vessel stowage
    planning. The locations of the MPP have coordinates (Bay, Deck).

    This environment simulates realistic stowage scenarios characterized by uncertain cargo demand, vessel capacity
    constraints, seaworthiness requirements, and operational cost minimization. It models the problem as a Markov
    Decision Process (MDP) and is designed for training and evaluating RL policies, such as AI2STOW.

    Key Features:
        - Demand uncertainty modeling across multiple time steps and ports.
        - Revenue optimization and cost evaluation, including penalties for overstowage and excess crane operations.
        - Projection layers to enforce feasibility under convex constraints.
        - Scalable interface suitable for realistic-sized vessel planning horizons.

    The environment provides a platform for testing ML4CO (Machine Learning for Combinatorial Optimization) methods
    with emphasis on the objective value, feasibility, and computational efficiency.

    For more details, please refer to the paper: https://arxiv.org/abs/2502.12756
    """
    name = "mpp"
    batch_locked = False

    def __init__(self, device="cuda", batch_size=(), td_gen=None, **kwargs):
        super().__init__(device=device, batch_size=batch_size)
        # Kwargs
        self.P = kwargs.get("ports") # Number of ports
        self.B = kwargs.get("bays")  # Number of bays
        self.D = kwargs.get("decks") # Number of decks
        self.T = int((self.P ** 2 - self.P) / 2) # Number of (POL,POD) transports
        self.CC = kwargs.get("customer_classes")  # Number of customer contracts
        self.K = kwargs.get("cargo_classes") * self.CC # Number of container classes
        self.W = kwargs.get("weight_classes")  # Number of weight classes
        self.stab_delta = kwargs.get("stability_difference")
        self.LCG_target = kwargs.get("LCG_target")
        self.VCG_target = kwargs.get("VCG_target")
        self.ho_costs = kwargs.get("hatch_overstowage_costs")
        self.cm_costs = kwargs.get("long_crane_costs")
        self.ho_mask = kwargs.get("hatch_overstowage_mask")
        self.CI_target = kwargs.get("CI_target")
        self.normalize_obs = kwargs.get("normalize_obs")
        self.limit_revenue = kwargs.get("limit_revenue", False)
        self.block_stowage_mask = False # must be false for this env

        ## Init env
        # Seed and generator
        self._set_seed(kwargs.get("seed"))
        self.demand_uncertainty = kwargs.get("demand_uncertainty", False)
        self.generator = UniformMPP_Generator(device=device, **kwargs) # MPP_Generator(device=device,**kwargs)
        if td_gen == None:
            self.td_gen = self.generator(batch_size=batch_size,)
        # Data type and shapes
        self.float_type = kwargs.get("float_type", th.float32)
        self.zero = th.tensor([0], device=self.device, dtype=self.float_type)
        self._compact_form_shapes()
        self._make_spec(self.td_gen)

        ## Sets and Parameters:
        self._precompute_transport_sets()
        self._initialize_capacity(*kwargs.get("capacity"))
        self.revenues_matrix = self._precompute_revenues()
        self._initialize_stability()
        self._initialize_step_parameters()
        self._initialize_constraints()


    def _make_spec(self, td:TensorDict = None) -> None:
        """Define the specs for observations, actions, rewards, and done flags."""
        batch_size = td.batch_size
        # observ = Unbounded(shape=(*batch_size,288), dtype=self.float_type) # 287, 307
        state_spec = Composite(
            # Demand
            observed_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            realized_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            expected_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            std_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            init_expected_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            batch_updates=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),
            # Vessel
            utilization=Unbounded(shape=(*batch_size,self.B*self.D*self.T*self.K), dtype=self.float_type),
            target_long_crane=Unbounded(shape=(*batch_size,1), dtype=self.float_type),
            long_crane_moves_discharge=Unbounded(shape=(*batch_size,self.B-1), dtype=self.float_type),
            lcg=Unbounded(shape=(*batch_size, 1), dtype=self.float_type),
            vcg=Unbounded(shape=(*batch_size, 1), dtype=self.float_type),
            residual_capacity=Unbounded(shape=(*batch_size, self.B * self.D),  dtype=self.float_type),
            residual_lc_capacity=Unbounded(shape=(*batch_size, self.B - 1), dtype=self.float_type),
            agg_pol_location=Unbounded(shape=(*batch_size, self.B * self.D), dtype=self.float_type),
            agg_pod_location=Unbounded(shape=(*batch_size, self.B * self.D), dtype=self.float_type),
            timestep=Unbounded(shape=(*batch_size, 1), dtype=th.int64),
            shape=batch_size,
        )
        self.observation_spec = Composite(
            # State, action, generator
            observation=state_spec,
            action=Unbounded(shape=(*batch_size, self.B * self.D), dtype=self.float_type),

            # Performance
            profit=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),
            revenue=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),
            cost=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),

            # Constraints
            clip_min=Unbounded(shape=(*batch_size,self.B*self.D),dtype=self.float_type),
            clip_max=Unbounded(shape=(*batch_size,self.B*self.D),dtype=self.float_type),
            lhs_A=Unbounded(shape=(*batch_size,self.n_constraints,self.B*self.D),dtype=self.float_type),
            rhs=Unbounded(shape=(*batch_size,self.n_constraints),dtype=self.float_type),
            violation=Unbounded(shape=(*batch_size,self.n_constraints),dtype=self.float_type),
            shape=batch_size,
        )
        self.action_spec = Bounded(
            shape=(*batch_size, self.B*self.D),  # Define shape as needed
            low=0.0,
            high=50.0,  # Define high value as needed
            dtype=self.float_type,
        )
        self.reward_spec = Unbounded(shape=(*batch_size,1,))
        self.done_spec = Unbounded(shape=(*batch_size,1,), dtype=th.bool)

    def _check_done(self, t: Tensor) -> Tensor:
        """Determine if the episode is done based on the state."""
        return (t == (self.T*self.K) - 1).view(-1,1)

    def _step(self, td: TensorDict) -> TensorDict:
        """Perform a step in the environment and return the next state."""
        # Extraction
        batch_size = td.batch_size
        action_state, demand_state, vessel_state, time = self._extract_from_td(td, batch_size)
        pol, pod, tau, k, rev, step = self._extract_cargo_parameters_for_step(time[0])

        # Get indices
        ac_transport, moves = self.remain_on_board_transport[pol], self.moves_idx[pol]

        # Check done
        done = self._check_done(time)

        # Update utilization
        vessel_state["utilization"] = update_state_loading(action_state["action"], vessel_state["utilization"], tau, k,)

        # Compute violation
        action_state["violation"] = self._compute_violation(
            action_state["lhs_A"], action_state["rhs"], action_state["action"],  batch_size)

        # Compute long crane moves & od-pairs
        vessel_state["long_crane_moves_load"] = compute_long_crane(vessel_state["utilization"], moves, self.T)
        vessel_state["pol_locations"], vessel_state["pod_locations"] = compute_pol_pod_locations(
            vessel_state["utilization"], self.transform_tau_to_pol, self.transform_tau_to_pod)
        vessel_state["agg_pol_location"], vessel_state["agg_pod_location"] = aggregate_pol_pod_location(
            vessel_state["pol_locations"], vessel_state["pod_locations"], self.float_type, block=False)

        # Compute total loaded
        sum_action = action_state["action"].sum(dim=(-2, -1)).unsqueeze(-1)

        # Compute reward & cost
        revenue = self._compute_revenue(sum_action, demand_state, rev)
        profit, cost = self._compute_cost(revenue, vessel_state, moves, ac_transport, step)

        # Transition to next step
        is_done = done.any()
        time = th.where(is_done, time, time + 1)
        next_state_dict = self._update_next_state(vessel_state, demand_state, action_state, time, batch_size, block=True, is_done=is_done)
        action_state = self._update_action_state(action_state, next_state_dict, step, time, batch_size, is_done=is_done)

        # Update performance metrics
        if is_done:
            # Compute crane cost at last port (only discharging)
            # NOTE: Use vessel_state["utilization"] and moves to compute crane moves at last port!
            lc_moves_last_port = compute_long_crane(vessel_state["utilization"], moves, self.T, block=True)
            cm_costs_last_port = compute_long_crane_excess_cost(lc_moves_last_port, next_state_dict["target_long_crane"], self.cm_costs)
            # Update profit and cost
            profit -= cm_costs_last_port
            cost += cm_costs_last_port
        # Compute the final reward after all adjustments.
        reward = self._compute_final_reward(revenue, cost, demand_state, step, time, batch_size)

        # Get output td
        out = TensorDict({
            "observation": {
                **flatten_values_td(next_state_dict, batch_size=batch_size),
                "total_profit": td["observation", "total_profit"] + profit,
                "max_total_profit":td["observation", "max_total_profit"] + self.revenues.max() * demand_state["current_demand"],
                "timestep": time,
            },
            **action_state,

            # Performance and environment
            "profit": profit,
            "revenue": revenue,
            "cost": cost,
            "reward": reward,
            "done": done,
        }, td.shape)
        return out

    def _reset(self,  td: Optional[TensorDict] = None, seed:Optional=None) -> TensorDict:
        """Reset the environment to the initial state."""
        # Extract batch_size from td if it exists
        if td is None: td = self.td_gen
        batch_size = getattr(td, 'batch_size', self.batch_size)
        td = self.generator(batch_size=batch_size, td=td)

        # Initialize
        # Parameters
        device = td.device
        if batch_size == torch.Size([]): time = th.zeros(1, dtype=th.int64, device=device)
        else: time = th.zeros(*batch_size, dtype=th.int64, device=device)
        pol, pod, tau, k, rev, step = self._extract_cargo_parameters_for_step(time[0])
        load_idx = self.load_transport[pol]

        # Demand state
        demand_state = td["observation"].clone().exclude("batch_updates", "init_expected_demand")
        realized_demand = td["observation", "realized_demand"].view(*batch_size, self.T, self.K).clone()
        if self.demand_uncertainty:
            observed = torch.zeros_like(realized_demand)
            observed[..., load_idx, :] = realized_demand[..., load_idx, :]
        else:
            observed = realized_demand.clone()
        demand_state["observed_demand"] = observed.view(*batch_size, self.T * self.K)
        current_demand = observed[..., tau, k].view(*batch_size, 1).clone()

        # Vessel state
        vessel_state = TensorDict({}, batch_size=batch_size, device=device)
        vessel_state["utilization"] = th.zeros((*batch_size, self.B, self.D, self.BL, self.T, self.K), device=device, dtype=self.float_type)
        vessel_state["residual_capacity"] = th.clamp(self.capacity - vessel_state["utilization"].sum(dim=-2) @ self.teus, min=self.zero)
        vessel_state["target_long_crane"] = compute_target_long_crane(realized_demand.to(self.float_type), self.moves_idx[time[0]],
                                                        self.capacity, self.B, self.CI_target).view(*batch_size, 1)
        vessel_state["residual_lc_capacity"] = vessel_state["target_long_crane"].repeat(1, self.B - 1)
        vessel_state["long_crane_moves_discharge"] = th.zeros_like(vessel_state["residual_lc_capacity"])
        vessel_state["agg_pol_location"] = th.zeros(self.action_spec.shape, dtype=self.float_type, device=device)
        vessel_state["agg_pod_location"] = th.zeros_like(vessel_state["agg_pol_location"])
        vessel_state["lcg"] = th.ones_like(time, dtype=self.float_type)
        vessel_state["vcg"] = th.ones_like(time, dtype=self.float_type)
        vessel_state["excess_pod_locations"] = th.zeros(*batch_size, self.B * self.BL, dtype=self.float_type)
        pod_locations = th.zeros((*batch_size, self.B, self.D, self.BL, self.P), dtype=self.float_type, device=device)

        # Action state
        action_state = TensorDict({}, batch_size=batch_size, device=device)
        action_state["action"] = th.zeros(self.action_spec.shape, dtype=self.float_type, device=device)
        action_state["clip_min"] = th.zeros(self.action_spec.shape, dtype=self.float_type, device=device)
        action_state["clip_max"] = vessel_state["residual_capacity"].view(*batch_size, self.n_locations)
        # todo: A_lhs,A_rhs not included in this paper - what does this change; check it!
        action_state["lhs_A"] = self.create_lhs_A(self.A_lhs, time).view(*batch_size, self.n_constraints, self.n_locations)
        action_state["rhs"] = self.create_rhs(
            vessel_state["utilization"].to(self.float_type), current_demand, self.swap_signs_stability,
            self.A_rhs, self.n_constraints, self.n_demand, self.n_locations, batch_size)
        action_state["violation"] = th.zeros_like(action_state["rhs"], dtype=self.float_type)
        if self.stowage_mask:
            action_mask = generate_POD_mask(realized_demand[..., tau, :] @ self.teus, vessel_state["residual_capacity"],
                                            self.capacity, pod_locations, pod, batch_size)
        else:
            action_mask = th.ones((*batch_size, self.n_locations), dtype=th.bool, device=device)

        # Init tds
        initial_state = TensorDict({
            **demand_state,
            **flatten_values_td(vessel_state, batch_size=batch_size),
            "timestep": time,
            "action_mask": action_mask,
            "total_profit":th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "max_total_profit":th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
        }, batch_size=batch_size, device=device,)

        # Init tds - full td
        out = TensorDict({
            "observation": initial_state,
            **action_state,
            # Performance and environment
            "profit": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "revenue": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "cost": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "done": th.zeros_like(time, dtype=th.bool).view(*batch_size, 1),
        }, batch_size=batch_size, device=device,)
        return out



        # # Demand:
        # realized_demand = td["observation", "realized_demand"].view(*batch_size, self.T, self.K).clone()
        # if self.demand_uncertainty:
        #     observed_demand = th.zeros_like(realized_demand)
        #     load_idx = self.load_transport[pol]
        #     observed_demand[..., load_idx, :] = realized_demand[..., load_idx, :]
        #     expected_demand = td["observation", "expected_demand"].clone()
        #     std_demand = td["observation", "std_demand"].clone()
        # else:
        #     observed_demand = realized_demand.clone()
        #     expected_demand = td["observation", "expected_demand"].clone()
        #     std_demand = td["observation", "std_demand"].clone()
        # current_demand = observed_demand[..., tau, k].view(*batch_size, 1).clone() # clone to prevent in-place!
        #
        # # State and mask
        # action_mask = th.ones((*batch_size, self.B*self.D), dtype=th.bool, device=device)
        # # Vessel
        # utilization = th.zeros((*batch_size, self.B, self.D, self.T, self.K), device=device, dtype=self.float_type)
        # residual_capacity = th.clamp(self.capacity - utilization.sum(dim=-2) @ self.teus, min=self.zero)
        # target_long_crane = compute_target_long_crane(realized_demand.to(self.float_type), self.moves_idx[time[0]],
        #                                                 self.capacity, self.B, self.CI_target).view(*batch_size, 1)
        # residual_lc_capacity = target_long_crane.repeat(1, self.B - 1)
        # locations_utilization = th.zeros_like(action_mask, dtype=self.float_type)
        #
        # # Constraints # todo: A_lhs,A_rhs not included in this paper - what does this change; check it!
        # lhs_A = self.create_lhs_A(self.A_lhs, time,)
        # rhs = self.create_rhs(utilization.to(self.float_type), current_demand, self.swap_signs_stability, self.A_rhs,
        #     self.n_constraints, self.n_demand, self.n_locations, batch_size)
        # # Init tds - state: internal state
        # # todo: hardcoded shapes; use spec instead
        # print(td["observation",])
        # breakpoint()
        #
        # initial_state = TensorDict({
        #     "timestep": time,
        #     # Demand
        #     "observed_demand": observed_demand.view(*batch_size, self.T * self.K),
        #     "realized_demand": td["observation", "realized_demand"].view(*batch_size, self.T * self.K),
        #     "expected_demand": expected_demand.view(*batch_size, self.T * self.K),
        #     "std_demand": std_demand.view(*batch_size, self.T * self.K),
        #     "init_expected_demand": td["observation", "init_expected_demand"].view(*batch_size, self.T * self.K),
        #     "batch_updates": td["observation", "batch_updates"],
        #     # Vessel
        #     "utilization": utilization.view(*batch_size, self.B * self.D * self.T * self.K),
        #     "target_long_crane": target_long_crane,
        #     "long_crane_moves_discharge": th.zeros_like(residual_lc_capacity).view(*batch_size, self.B - 1),
        #     "residual_capacity": residual_capacity.view(*batch_size, self.B * self.D),
        #     "lcg": th.ones_like(time, dtype=self.float_type).view(*batch_size, 1),
        #     "vcg": th.ones_like(time, dtype=self.float_type).view(*batch_size, 1),
        #     "residual_lc_capacity": residual_lc_capacity.view(*batch_size, self.B - 1),
        #     "agg_pol_location": th.zeros_like(locations_utilization),
        #     "agg_pod_location": th.zeros_like(locations_utilization),
        # }, batch_size=batch_size, device=device,)
        #
        # # Init tds - full td
        # out = TensorDict({
        #     # State
        #     "observation": initial_state,
        #     # # Action mask
        #     "action": th.zeros_like(action_mask, dtype=self.float_type),
        #     # "action_mask": action_mask.view(*batch_size, -1),
        #     # # Constraints
        #     "clip_min": th.zeros_like(residual_capacity, dtype=self.float_type).view(*batch_size, self.B * self.D, ),
        #     "clip_max": residual_capacity.view(*batch_size, self.B * self.D),
        #     "lhs_A": lhs_A.view(*batch_size, self.n_constraints, self.B * self.D),
        #     "rhs":  rhs.view(*batch_size, self.n_constraints),
        #     "violation": th.zeros_like(rhs, dtype=self.float_type),
        #     # Performance
        #     "profit": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
        #     "revenue": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
        #     "cost": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
        #     # Reward, done and step
        #     "done": th.zeros_like(time, dtype=th.bool).view(*batch_size, 1),
        # }, batch_size=batch_size, device=device,)
        # return out

    def _set_seed(self, seed: Optional[int] = None) -> int:
        """
        Sets the seed for the environment and updates the RNG.

        Args:
            seed (Optional[int]): The seed to use. If None, a random seed is generated.

        Returns:
            int: The seed used to initialize the RNG.
        """
        self.rng = torch.Generator(device=self.device)
        if seed is None:
            seed = self.rng.seed()
        self.rng.manual_seed(seed)
        self.seed = seed
        return seed

    # Extraction functions
    def _extract_from_td(self, td:TensorDict, batch_size:Tuple) -> Tuple:
        """Extract action, reward and step from the TensorDict."""
        # Must clone to avoid in-place operations!
        timestep = td["observation", "timestep"].view(-1).clone()

        # Action-related variables
        action = {
            "action": td["action"].view(*batch_size, self.B, self.D,).clone(),
            # "action_mask": td["action_mask"].view(*batch_size, self.B, self.D,).clone(),
            "lhs_A": td["lhs_A"].view(*batch_size, self.n_constraints, self.B*self.D,).clone(),
            "rhs": td["rhs"].view(*batch_size, self.n_constraints,).clone(),
        }

        # Demand-related variables
        demand = {
            # clones are needed to prevent in-place
            "expected_demand": td["observation", "expected_demand"].view(*batch_size, self.T, self.K).clone(),
            "std_demand": td["observation", "std_demand"].view(*batch_size, self.T, self.K).clone(),
            "realized_demand": td["observation", "realized_demand"].view(*batch_size, self.T, self.K).clone(),
            "observed_demand": td["observation", "observed_demand"].view(*batch_size, self.T, self.K).clone(),
            "current_demand": td["observation", "realized_demand"].clone()[..., timestep[0]].view(*batch_size, 1),
        }
        # Vessel-related variables
        vessel = {
            "utilization": td["observation", "utilization"].view(*batch_size, self.B, self.D, self.T, self.K).clone(),
            "target_long_crane": td["observation", "target_long_crane"].view(*batch_size, 1).clone(),
            "long_crane_moves_discharge": td["observation", "long_crane_moves_discharge"].view(*batch_size, self.B - 1).clone(),
            "residual_capacity": td["observation", "residual_capacity"].view(*batch_size, self.B, self.D,).clone(),
        }
        return action, demand, vessel, timestep

    def _extract_cargo_parameters_for_step(self, time:Tensor) -> Tuple:
        """Extract cargo-related parameters"""
        pol = self.pol[time]
        pod = self.pod[time]
        k = self.k[time]
        tau = self.tau[time]
        rev_t = self.revenues[time]
        step = self.steps[time]
        return pol, pod, tau, k, rev_t, step


    def _get_observation(self, next_state_dict:Dict, residual_capacity:Tensor,
                         agg_pol_location:Tensor, agg_pod_location:Tensor, time:Tensor, batch_size:Tuple) -> Tensor:
        """Get observation from the TensorDict."""
        if self.normalize_obs:
            # Normalize demand and clip max demand based on train range
            max_demand = next_state_dict["realized_demand"].max() if self.generator.train_max_demand == None else self.generator.train_max_demand
            out = th.cat([
                time.view(*batch_size, 1) / (self.T * self.K),
                next_state_dict["observed_demand"].view(*batch_size, self.T * self.K) / max_demand,
                next_state_dict["expected_demand"].view(*batch_size, self.T * self.K) / max_demand,
                next_state_dict["std_demand"].view(*batch_size, self.T * self.K) / max_demand,
                next_state_dict["lcg"].view(*batch_size, 1),
                next_state_dict["vcg"].view(*batch_size, 1),
                (residual_capacity/self.capacity.unsqueeze(0)).view(*batch_size, self.B * self.D),
                (next_state_dict["residual_lc_capacity"]/next_state_dict["target_long_crane"].unsqueeze(0)).view(*batch_size, self.B - 1),
                agg_pol_location.view(*batch_size, self.B * self.D) / (self.P),
                agg_pod_location.view(*batch_size, self.B * self.D) / (self.P),
            ], dim=-1)
        else:
            out = th.cat([
                time.view(*batch_size, 1),
                next_state_dict["observed_demand"].view(*batch_size, self.T * self.K),
                next_state_dict["expected_demand"].view(*batch_size, self.T * self.K),
                next_state_dict["std_demand"].view(*batch_size, self.T * self.K),
                next_state_dict["lcg"].view(*batch_size, 1),
                next_state_dict["vcg"].view(*batch_size, 1),
                residual_capacity.view(*batch_size, self.B * self.D),
                next_state_dict["residual_lc_capacity"].view(*batch_size, self.B - 1),
                agg_pol_location.view(*batch_size, self.B * self.D),
                agg_pod_location.view(*batch_size, self.B * self.D),
            ], dim=-1)
        return out

    # Update state
    def _update_next_state(self, vessel_state:Dict, demand_state:Dict, action_state:Dict,
                           time:Tensor, batch_size:Tuple, block:bool=False, is_done:Tensor=False) -> Union[Dict[str, Tensor],TensorDict]:
        """Update next state, following options:
        - Next step moves to new port POL+1
        - Next step moves to new transport (POL, POD-1)
        - Last step of episode; compute excess crane moves at last port
        """
        # Get cargo parameters
        pol, pod, tau, k, _, _ = self._extract_cargo_parameters_for_step(time[0])

        # Check next port with t - 1
        load_idx, disc_idx, moves_idx = self._precompute_for_step(pol)
        # Next port with discharging; Update utilization, observed demand and target long crane
        if self.next_port_mask[time-1].any():
            vessel_state["long_crane_moves_load"] = torch.zeros_like(vessel_state["long_crane_moves_load"])
            vessel_state["long_crane_moves_discharge"] = compute_long_crane(
                vessel_state["utilization"], moves_idx, self.T, block=block)
            vessel_state["utilization"] = update_state_discharge(vessel_state["utilization"], disc_idx)
            vessel_state["target_long_crane"] = compute_target_long_crane(
                demand_state["realized_demand"], moves_idx, self.capacity, self.B, self.CI_target).view(*batch_size, 1)
            if self.demand_uncertainty:
                demand_state["observed_demand"][..., load_idx, :] = demand_state["realized_demand"][..., load_idx, :]

        # Update residual capacity
        vessel_state["residual_capacity"] = self._compute_residual_capacity(vessel_state["utilization"]) if not is_done else \
            torch.zeros_like(vessel_state["residual_capacity"], dtype=self.float_type).view(*batch_size, self.B,self.D,self.BL )

        # Compute action mask
        if k == 0 and self.block_stowage_mask:
            action_state["action_mask"] = generate_POD_mask(
                demand_state["realized_demand"][..., tau, :] @ self.teus,
                vessel_state["residual_capacity"], self.capacity,
                vessel_state["pod_locations"], pod, batch_size)
        action_mask = action_state.pop("action_mask")

        # Update residual lc capacity: target - actual load and discharge moves
        vessel_state["long_crane_moves"] = vessel_state["long_crane_moves_load"] + vessel_state["long_crane_moves_discharge"]
        residual_lc_capacity = (vessel_state["target_long_crane"] - vessel_state["long_crane_moves"]).clamp(min=0)

        # Compute stability
        lcg, vcg = compute_stability(vessel_state["utilization"], self.weights, self.longitudinal_position, self.vertical_position, block=block)

        # Get output
        return TensorDict({
            "current_demand": demand_state["realized_demand"][..., tau, k],
            "observed_demand": demand_state["observed_demand"],
            "expected_demand": demand_state["expected_demand"],
            "std_demand":demand_state["std_demand"],
            "realized_demand": demand_state["realized_demand"],
            "utilization": vessel_state["utilization"],
            "residual_capacity": vessel_state["residual_capacity"],
            "lcg": lcg,
            "vcg": vcg,
            "target_long_crane": vessel_state["target_long_crane"],
            "residual_lc_capacity": residual_lc_capacity,
            "long_crane_moves_discharge": vessel_state["long_crane_moves_discharge"],
            "agg_pol_location": vessel_state["agg_pol_location"],
            "agg_pod_location": vessel_state["agg_pod_location"],
            "excess_pod_locations": vessel_state["excess_pod_locations"],
            "action_mask": action_mask,
        }, batch_size=batch_size, device=self.device)

    def _update_action_state(self, action_state:Dict, next_state_dict:TensorDict,
                             step:Tensor, time:Tensor, batch_size:Tuple[int,...], is_done:Tensor) -> Dict:
        """Update action state for the next step."""
        current_demand = next_state_dict.pop("current_demand")
        if not is_done:
            # Update feasibility constraints
            action_state["lhs_A"] = self.create_lhs_A(self.block_A_lhs, time).view(*batch_size, self.n_constraints, self.n_block_locations)
            action_state["rhs"] = self.create_rhs(
                next_state_dict["utilization"], current_demand, self.swap_signs_block_stability,
                self.block_A_rhs, self.n_constraints, self.n_demand, self.n_block_locations, batch_size)

        # Update action state
        action_state["clip_max"] = self._compute_clip_max(next_state_dict["residual_capacity"], current_demand, batch_size, step)
        action_state["action"] = action_state["action"].view(*batch_size, self.n_block_locations)
        return action_state

    # def _update_performance_metrics(self, profit:Tensor, revenue:Tensor,  cost:Tensor, demand_state:Dict, step:Tensor,
    #                                 time:Tensor, batch_size:Tuple) -> Tensor:
    #     """Update performance metrics for the next step."""
    #
    #     return reward, profit, cost, total_profit, max_total_profit


    # Compact formulation
    def _compact_form_shapes(self, ) -> None:
        """Define shapes for compact form"""
        self.n_demand = 1
        self.n_stability = 4
        self.n_locations = self.B * self.D
        self.n_constraints = self.n_demand + self.n_locations + self.n_stability

    def _create_constraint_matrix(self, shape: Tuple[int, int, int, int], rhs:bool=True) -> Tensor:
        """Create constraint matrix A for compact constraints Au <= b"""
        # [1, LM-TW, TW-LM, VM-TW, TW-VM]
        A = th.ones(shape, device=self.device, dtype=self.float_type)
        scaling = self.teus.view(1, 1, 1, -1) if rhs else 1
        A[self.n_demand:self.n_locations + self.n_demand,] *= scaling * th.eye(self.n_locations, device=self.device, dtype=self.float_type).view(self.n_locations, self.B*self.D, 1, 1)
        A *= self.constraint_signs.view(-1, 1, 1, 1)
        A[self.n_locations + self.n_demand:self.n_locations + self.n_demand + self.n_stability] *= self.stability_params_lhs.view(self.n_stability, self.B*self.D, 1, self.K,)
        return A.view(self.n_constraints, self.B*self.D, -1)

    def create_lhs_A(self, A:Tensor, time:Tensor) -> Tensor:
        """Get A_t based on batch of steps to prevent expanding A_t for each step"""
        steps = self.steps[time]
        return A[..., steps].permute((2, 0, 1,)).contiguous()

    def create_rhs(self, utilization:Tensor, current_demand:Tensor,
                   swap_signs_stability:Tensor, input_A:Tensor,
                   n_constraints:int, n_demand:int, n_locations:int, batch_size:Tuple) -> Tensor:
        """Create b_t based on current utilization:
        - b_t = [current_demand, capacity, LM_ub, LM_lb, VM_ub, VM_lb]
        - demand -> stepwise current demand [#]
        - capacity -> residual capacity [TEUs]
        - stability -> lower and upper bounds for LCG, VCG
        """
        # Perform matmul to get initial rhs, including:
        # note: utilization, A, teus_episode have static shapes
        A = swap_signs_stability.view(-1, 1, 1,) * input_A.clone() # Swap signs for constraints
        rhs = utilization.view(*batch_size, -1) @ A.view(n_constraints, -1).T
        # Update rhs with current demand and add teu capacity to the rhs
        rhs[..., :n_demand] = current_demand.view(-1, 1)
        rhs[..., n_demand:n_locations + n_demand] = \
            torch.clamp(rhs[..., n_demand:n_locations + n_demand] + self.capacity.view(1, -1),
                        min=th.zeros_like(self.capacity.view(1, -1)), max=self.capacity.view(1, -1))
        return rhs

    # Initializations
    def _initialize_capacity(self, capacity:float,) -> None:
        """Initialize capacity (TEU) parameters"""
        self.capacity = th.full((self.B, self.D,), capacity, device=self.device, dtype=self.float_type)
        self.total_capacity = th.sum(self.capacity)
        self.teus = th.arange(1, self.K // (self.CC * self.W) + 1, device=self.device, dtype=self.float_type) \
            .repeat_interleave(self.W).repeat(self.CC)
        self.teus_episode = th.cat([self.teus.repeat(self.T)])

    def _initialize_stability(self, ) -> None:
        """Initialize stability parameters"""
        self.weights = th.arange(1, self.W + 1, device=self.device, dtype=self.float_type).repeat(self.K // self.W)
        self.longitudinal_position = th.arange(1 / self.B, self.B * 2 / self.B, 2 / self.B, device=self.device,
                                               dtype=self.float_type)
        self.vertical_position = th.arange(1 / self.D, self.D * 2 / self.D, 2 / self.D, device=self.device,
                                           dtype=self.float_type)
        self.lp_weight = th.einsum("d, b -> bd", self.weights, self.longitudinal_position).unsqueeze(0)
        self.vp_weight = th.einsum("d, c -> cd", self.weights, self.vertical_position).unsqueeze(0)
        self.stability_params_lhs = self._precompute_stability_parameters()

    def _initialize_step_parameters(self, ) -> None:
        """Initialize step parameters"""
        self.steps = self._precompute_order_standard()
        self.k, self.tau = get_k_tau_pair(self.steps, self.K)
        self.pol, self.pod = get_pol_pod_pair(self.tau, self.P)
        self.revenues = self.revenues_matrix[self.k, self.tau]
        self._precompute_transport_sets_episode()
        self.next_port_mask = self._precompute_next_port_mask()
        self.transform_tau_to_pol = get_pols_from_transport(self.transport_idx, self.P, dtype=self.float_type)
        self.transform_tau_to_pod = get_pods_from_transport(self.transport_idx, self.P, dtype=self.float_type)

    def _initialize_constraints(self, ) -> None:
        """Initialize constraint-related parameters."""
        self.constraint_signs = th.ones(self.n_constraints, device=self.device, dtype=self.float_type)
        self.constraint_signs[th.tensor([-3, -1], device=self.device)] *= -1  # Flip signs for specific constraints

        # Swap signs for stability constraints, only the first one remains positive
        self.swap_signs_stability = -th.ones_like(self.constraint_signs)
        self.swap_signs_stability[0] = 1

        # Create constraint matrix
        self.A_lhs = self._create_constraint_matrix(shape=(self.n_constraints, self.n_locations, self.T, self.K), rhs=False)
        self.A_rhs = self._create_constraint_matrix(shape=(self.n_constraints, self.n_locations, self.T, self.K), rhs=True)

    # Precomputes
    def _precompute_order_standard(self) -> Tensor:
        """Get standard order of steps;
        - POL, POD are in ascending order
        - K is in ascending order but based on priority"""
        return th.arange(self.T*self.K, device=self.device, dtype=th.int64)

    def _precompute_for_step(self, pol:Tensor) -> Tuple[Tensor,Tensor,Tensor]:
        """Precompute variables and index masks for the current step"""
        # Index masks
        load_idx = self.load_transport[pol]
        disc_idx = self.discharge_transport[pol]
        moves_idx = self.moves_idx[pol]
        return load_idx, disc_idx, moves_idx

    def _precompute_revenues(self, reduce_long_revenue:float=0.3) -> Tensor:
        """Precompute matrix of revenues with shape [K, T]"""
        # Initialize revenues and pod_grid
        revenues = th.zeros((self.K, self.P, self.P), device=self.device, dtype=self.float_type) # Shape: [K, P, P]
        self.ports = torch.arange(self.P, device=self.device)
        pol_grid, pod_grid = th.meshgrid(self.ports, self.ports, indexing='ij')  # Shapes: [P, P]
        duration_grid = (pod_grid-pol_grid).to(revenues.dtype) # Shape: [P, P]
        # Compute revenues
        mask = th.arange(self.K, device=self.device, dtype=self.float_type) < self.K // 2 # Spot/long-term mask
        revenues[~mask] = duration_grid # Spot market contracts
        revenues[mask] = (duration_grid * (1 - reduce_long_revenue)) # Long-term contracts
        i, j = th.triu_indices(self.P, self.P, offset=1) # Get above-diagonal indices of revenues
        # Add 0.1 for variable revenue per container, regardless of (k,tau)
        return revenues[..., i, j] + 0.1 # Shape: [K, T], where T = P*(P-1)/2

    def _precompute_transport_sets(self) -> None:
        """Precompute transport sets based on POL with shape(s): [P, T]"""
        # Note: transport sets in the environment do not depend on batches for efficiency.
        # Hence, implementation only works for batches with the same episodic step (e.g., single-step MDP)
        self.transport_idx = get_transport_idx(self.P, device=self.device)

        # Get transport sets for demand
        p = th.arange(self.P, device=self.device, dtype=self.float_type).view(-1,1)
        self.load_transport = get_load_transport(self.transport_idx, p)
        self.previous_load_transport = get_load_transport(self.transport_idx, p-1)
        self.discharge_transport = get_discharge_transport(self.transport_idx, p)
        self.not_on_board_transport = get_not_on_board_transport(self.transport_idx, p)
        self.remain_on_board_transport = get_remain_on_board_transport(self.transport_idx, p)
        self.moves_idx = self.load_transport + self.discharge_transport

    def _precompute_transport_sets_episode(self) -> None:
        """Precompute transport sets based on POL with shape(s): [Seq, T]"""
        # Get transport sets for demand
        pol_t = self.pol[:-1].view(-1,1)
        self.load_transport_episode = get_load_transport(self.transport_idx, pol_t)
        self.previous_load_transport_episode = get_load_transport(self.transport_idx, pol_t-1)
        self.discharge_transport_episode = get_discharge_transport(self.transport_idx, pol_t)
        self.not_on_board_transport_episode = get_not_on_board_transport(self.transport_idx, pol_t)
        self.remain_on_board_transport_episode = get_remain_on_board_transport(self.transport_idx, pol_t)
        self.moves_idx_episode = self.load_transport_episode + self.discharge_transport_episode

    def _precompute_next_port_mask(self) -> Tensor:
        """Precompute next port based on POL with shape: [P, T]
        - Next port happens when POD = POL+1
        """
        # Initialize next_port
        next_port = th.zeros((self.T*self.K), dtype=th.bool, device=self.device)
        pol_values = th.arange(self.P - 1, 0, -1,)  # POL values from P-1 to 1
        indices = th.cumsum(self.K * pol_values, dim=0) - 1
        next_port[indices] = True
        return next_port

    def _precompute_stability_parameters(self,) -> Tensor:
        """Precompute lhs stability parameters for compact constraints. Get rhs by negating lhs."""
        lp_weight = self.lp_weight.unsqueeze(2).expand(-1,-1,self.D,-1)
        vp_weight = self.vp_weight.unsqueeze(1).expand(-1,self.B,-1,-1)
        p_weight = th.cat([lp_weight, lp_weight, vp_weight, vp_weight], dim=0)
        target = torch.tensor([self.LCG_target, self.LCG_target, self.VCG_target, self.VCG_target],
                              device=self.device, dtype=self.float_type).view(-1,1,1,1)
        delta = torch.tensor([self.stab_delta, -self.stab_delta, self.stab_delta, -self.stab_delta],
                             device=self.device, dtype=self.float_type).view(-1,1,1,1)
        output = p_weight - self.weights.view(1,1,1,self.K) * (target + delta)
        return output.view(-1, self.B*self.D, self.K,)

    # Compute functions
    def _compute_violation(self, lhs_A:Tensor, rhs:Tensor, action:Tensor, batch_size:Tuple) -> Tensor:
        if lhs_A.dim() == 2:
            violation = lhs_A @ action.view(*batch_size, -1) - rhs
        elif lhs_A.dim() == 3:
            violation = torch.bmm(lhs_A, action.view(*batch_size, -1, 1)) - rhs.unsqueeze(-1)
        else:
            raise ValueError("lhs_A has wrong dimensions.")

        return torch.clamp(violation, min=0).view(*batch_size, -1)

    def _compute_revenue(self, sum_action:Tensor, demand_state:TensorDict, rev:Tensor) -> Tensor:
        if self.limit_revenue:
            return torch.clamp(sum_action, min=self.zero, max=demand_state["current_demand"]) * rev
        return sum_action * rev

    def _compute_cost(self, revenue:Tensor, vessel_state:TensorDict, moves:Tensor,
                      ac_transport:Tensor, step:Tensor, block:bool=False) -> Tuple[Tensor, Tensor]:
        """Compute profit based on revenue and cost, where cost = overstowage costs + excess_crane_moves costs.
        Costs are based on utilization, long crane moves and target long crane"""
        profit = revenue.clone()
        if self.next_port_mask[step].any():
            # Compute overstowage costs
            overstowage = compute_hatch_overstowage(vessel_state["utilization"], moves, ac_transport, block)
            ho_costs = overstowage.sum(dim=-1, keepdim=True) * self.ho_costs
            # Compute crane move costs
            cm_costs = compute_long_crane_excess_cost(vessel_state["long_crane_moves_discharge"], vessel_state["target_long_crane"], self.cm_costs)            
            cost = ho_costs + cm_costs
            profit -= cost
        else:
            cost = th.zeros_like(profit)
        return profit, cost

    def _compute_final_reward(self, revenue: Tensor, cost: Tensor, demand_state: TensorDict,
                              step: Tensor, time: Tensor, batch_size: Tuple) -> Tensor:
        """
        Compute the final reward as the difference between normalized revenue and normalized cost.
        - Revenue is normalized by the maximum possible revenue per timestep and adjusted by current demand.
        - Cost is normalized by the cumulative demand realized up to the current step and scaled by expected time.
        - Final reward reflects normalized revenue per port. We have spikes of delayed costs at final step of port,
            but per port the revenue and costs are normalized.
        """

        # Normalization revenue: max potential revenue weighted by current demand
        norm_revenue = self.revenues.max() * demand_state["current_demand"]

        # Normalization cost:
        #   - Sum realized demand up to and including current step
        #   - Scale by time to account for port duration
        #   - todo: assumes use _precompute_order_standard; needs adjustment for general logic
        norm_cost = demand_state["realized_demand"].view(*batch_size, -1)[..., :step + 1].sum(dim=-1, keepdims=True) / time[0]

        # Final reward: difference between normalized revenue and normalized cost
        return (revenue.clone() / norm_revenue) - (cost.clone() / norm_cost)

    def _compute_residual_capacity(self, utilization:Tensor) -> Tensor:
        """Compute residual capacity based on utilization"""
        return th.clamp(self.capacity - utilization.sum(dim=-2) @ self.teus, min=self.zero)

    def _compute_clip_max(self, residual_capacity:Tensor, current_demand:Tensor, batch_size:Tuple, step:Tensor) -> Tensor:
        """Compute clip max based on residual capacity and next state"""
        dims = residual_capacity.dim()-1 if residual_capacity.dim() > 3 else 2 # if dim <= 3, then 2D locations
        teu = self.teus_episode[step].view((1,) * dims)
        out = (residual_capacity / teu).view(*batch_size, self.action_spec.shape[-1])
        out = out.clamp(max=current_demand.view(*batch_size, 1))
        return out

class BlockMasterPlanningEnv(MasterPlanningEnv):
    """Reinforcement learning environment for solving the Master Planning Problem (MPP) in container vessel stowage
    planning. The environment extends the MasterPlanningEnv class and implements a block-based stowage
    representation, hence the locations of the MPP have coordinates (Bay, Deck, Block).


    This environment simulates realistic stowage scenarios characterized by uncertain cargo demand, vessel capacity
    constraints, seaworthiness requirements, paired block stowage patterns, and operational cost minimization. It
    models the problem as a Markov Decision Process (MDP) and is designed for training and evaluating  RL policies,
    such as AI2STOW.

    Key Features:
        - Block-based stowage representation, including support for paired block stowage patterns.
        - Demand uncertainty modeling across multiple time steps and ports.
        - Revenue optimization and cost evaluation, including penalties for overstowage and excess crane operations.
        - Action masking for non-convex constraints, and projection layers to enforce convex constraints.
        - Scalable interface suitable for realistic-sized vessel planning horizons.

    The environment provides a platform for testing ML4CO (Machine Learning for Combinatorial Optimization) methods
    with emphasis on the objective value, feasibility, and computational efficiency.

    For more details, please refer to the original paper: https://arxiv.org/abs/2504.04469
    """
    name = "block_mpp"
    batch_locked = False

    def __init__(self, device="cuda", batch_size=(), td_gen=None, **kwargs):
        # Kwargs and super
        self.BL = kwargs.get("blocks", 2)  # Number of paired blocks: 2 (wings + center), 3 (wings + center1 + center2)
        super().__init__(device=device, batch_size=batch_size, **kwargs)

        # Shapes
        self._compact_form_block_shapes()
        self._make_block_spec(self.td_gen)

        ## Sets and Parameters:
        self.block_stowage_mask = kwargs.get("block_stowage_mask", False)
        self._initialize_block_capacity(*kwargs.get("capacity"))
        self._initialize_block_stability()
        self._initialize_block_constraints()

    def _step(self, td: TensorDict) -> TensorDict:
        """Perform a step in the environment and return the next state."""
        # Extraction
        batch_size = td.batch_size
        action_state, demand_state, vessel_state, time = self._extract_from_block_td(td, batch_size)
        pol, pod, tau, k, rev, step = self._extract_cargo_parameters_for_step(time[0])

        # Get indices
        ac_transport, moves = self.remain_on_board_transport[pol], self.moves_idx[pol]

        # Check done
        done = self._check_done(time)

        # Update utilization
        vessel_state["utilization"] = update_state_loading(action_state["action"], vessel_state["utilization"], tau, k, )

        # Compute violation
        action_state["violation"] = self._compute_violation(action_state["lhs_A"], action_state["rhs"], action_state["action"], batch_size)

        # Compute long crane moves & od-pairs
        vessel_state["long_crane_moves_load"] = compute_long_crane(vessel_state["utilization"], moves, self.T, block=True)
        vessel_state["pol_locations"], vessel_state["pod_locations"] = \
            compute_pol_pod_locations(vessel_state["utilization"], self.transform_tau_to_pol, self.transform_tau_to_pod)
        vessel_state["agg_pol_location"], vessel_state["agg_pod_location"] = \
            aggregate_pol_pod_location(vessel_state["pol_locations"], vessel_state["pod_locations"], self.float_type, block=True)

        # Compute unique number of pods at each bay,block
        vessel_state["excess_pod_locations"] = th.clamp((vessel_state["pod_locations"].sum(dim=-3) > 0).sum(dim=-1) - 1, min=0.0)

        # Compute total loaded
        sum_action = action_state["action"].sum(dim=(-3, -2, -1)).unsqueeze(-1)

        # Compute reward & cost
        revenue = self._compute_revenue(sum_action, demand_state, rev)
        profit, cost = self._compute_cost(revenue, vessel_state, moves, ac_transport, step, block=True)

        # Transition to next step
        is_done = done.any()
        time = th.where(is_done, time, time + 1)
        next_state_dict = self._update_next_state(vessel_state, demand_state, action_state, time, batch_size, block=True, is_done=is_done)
        action_state = self._update_action_state(action_state, next_state_dict, step, time, batch_size, is_done=is_done)

        # Update performance metrics
        if is_done:
            # Compute crane cost at last port (only discharging)
            # NOTE: Use vessel_state["utilization"] and moves to compute crane moves at last port!
            lc_moves_last_port = compute_long_crane(vessel_state["utilization"], moves, self.T, block=True)
            cm_costs_last_port = compute_long_crane_excess_cost(lc_moves_last_port, next_state_dict["target_long_crane"], self.cm_costs)
            # Update profit and cost
            profit -= cm_costs_last_port
            cost += cm_costs_last_port
        # Compute the final reward after all adjustments.
        reward = self._compute_final_reward(revenue, cost, demand_state, step, time, batch_size)

        # Get output td
        out = TensorDict({
            "observation": {
                **flatten_values_td(next_state_dict, batch_size=batch_size),
                "total_profit": td["observation", "total_profit"] + profit,
                "max_total_profit":td["observation", "max_total_profit"] + self.revenues.max() * demand_state["current_demand"],
                "timestep": time,
            },
            **action_state,

            # Performance and environment
            "profit": profit,
            "revenue": revenue,
            "cost": cost,
            "reward": reward,
            "done": done,
        }, td.shape)
        return out

    def _reset(self,  td: Optional[TensorDict] = None, seed:Optional=None) -> TensorDict:
        """Reset the environment to the initial state."""
        # Extract batch_size from td if it exists
        if td is None: td = self.td_gen
        batch_size = getattr(td, 'batch_size', self.batch_size)
        td = self.generator(batch_size=batch_size, td=td)

        # Initialize
        # Parameters and indices:
        device = td.device
        if batch_size == torch.Size([]): time = th.zeros(1, dtype=th.int64, device=device)
        else: time = th.zeros(*batch_size, dtype=th.int64, device=device)
        pol, pod, tau, k, rev, step = self._extract_cargo_parameters_for_step(time[0])
        load_idx = self.load_transport[pol]

        # Demand state
        demand_state = td["observation"].clone().exclude("batch_updates", "init_expected_demand")
        realized_demand = td["observation", "realized_demand"].view(*batch_size, self.T, self.K).clone()
        if self.demand_uncertainty:
            observed = torch.zeros_like(realized_demand)
            observed[..., load_idx, :] = realized_demand[..., load_idx, :]
        else:
            observed = realized_demand.clone()
        demand_state["observed_demand"] = observed.view(*batch_size, self.T * self.K)
        current_demand = observed[..., tau, k].view(*batch_size, 1).clone()

        # Vessel state
        vessel_state = TensorDict({}, batch_size=batch_size, device=device)
        vessel_state["utilization"] = th.zeros((*batch_size, self.B, self.D, self.BL, self.T, self.K), device=device, dtype=self.float_type)
        vessel_state["residual_capacity"] = th.clamp(self.capacity - vessel_state["utilization"].sum(dim=-2) @ self.teus, min=self.zero)
        vessel_state["target_long_crane"] = compute_target_long_crane(realized_demand.to(self.float_type), self.moves_idx[time[0]],
                                                        self.capacity, self.B, self.CI_target).view(*batch_size, 1)
        vessel_state["residual_lc_capacity"] = vessel_state["target_long_crane"].repeat(1, self.B - 1)
        vessel_state["long_crane_moves_discharge"] = th.zeros_like(vessel_state["residual_lc_capacity"])
        vessel_state["agg_pol_location"] = th.zeros(self.action_spec.shape, dtype=self.float_type, device=device)
        vessel_state["agg_pod_location"] = th.zeros_like(vessel_state["agg_pol_location"])
        vessel_state["lcg"] = th.ones_like(time, dtype=self.float_type)
        vessel_state["vcg"] = th.ones_like(time, dtype=self.float_type)
        vessel_state["excess_pod_locations"] = th.zeros(*batch_size, self.B * self.BL, dtype=self.float_type)
        pod_locations = th.zeros((*batch_size, self.B, self.D, self.BL, self.P), dtype=self.float_type, device=device)

        # Action state
        action_state = TensorDict({}, batch_size=batch_size, device=device)
        action_state["action"] = th.zeros(self.action_spec.shape, dtype=self.float_type, device=device)
        action_state["clip_min"] = th.zeros(self.action_spec.shape, dtype=self.float_type, device=device)
        action_state["clip_max"] = vessel_state["residual_capacity"].view(*batch_size, self.n_block_locations)
        action_state["lhs_A"] = self.create_lhs_A(self.block_A_lhs, time).view(*batch_size, self.n_constraints, self.n_block_locations)
        action_state["rhs"] = self.create_rhs(
            vessel_state["utilization"].to(self.float_type), current_demand, self.swap_signs_block_stability,
            self.block_A_rhs, self.n_constraints, self.n_demand, self.n_block_locations, batch_size)
        action_state["violation"] = th.zeros_like(action_state["rhs"], dtype=self.float_type)
        if self.block_stowage_mask:
            action_mask = generate_POD_mask(realized_demand[..., tau, :] @ self.teus, vessel_state["residual_capacity"],
                                            self.capacity, pod_locations, pod, batch_size)
        else:
            action_mask = th.ones((*batch_size, self.n_block_locations), dtype=th.bool, device=device)

        # Init tds
        initial_state = TensorDict({
            **demand_state,
            **flatten_values_td(vessel_state, batch_size=batch_size),
            "timestep": time,
            "action_mask": action_mask,
            "total_profit":th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "max_total_profit":th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
        }, batch_size=batch_size, device=device,)

        # Init tds - full td
        out = TensorDict({
            "observation": initial_state,
            **action_state,
            # Performance and environment
            "profit": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "revenue": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "cost": th.zeros_like(time, dtype=self.float_type).view(*batch_size, 1),
            "done": th.zeros_like(time, dtype=th.bool).view(*batch_size, 1),
        }, batch_size=batch_size, device=device,)
        return out

    def _make_block_spec(self, td:TensorDict = None) -> None:
        """Define the specs for observations, actions, rewards, and done flags."""
        batch_size = td.batch_size
        state_spec = Composite(
            # Demand
            observed_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            realized_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            expected_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            std_demand=Unbounded(shape=(*batch_size, self.T * self.K), dtype=torch.float32),
            # Vessel
            utilization=Unbounded(shape=(*batch_size,self.B*self.D*self.BL*self.T*self.K), dtype=self.float_type),
            target_long_crane=Unbounded(shape=(*batch_size,1), dtype=self.float_type),
            long_crane_moves_discharge=Unbounded(shape=(*batch_size,self.B-1), dtype=self.float_type),
            lcg=Unbounded(shape=(*batch_size, 1), dtype=self.float_type),
            vcg=Unbounded(shape=(*batch_size, 1), dtype=self.float_type),
            residual_capacity=Unbounded(shape=(*batch_size, self.n_block_locations),  dtype=self.float_type),
            residual_lc_capacity=Unbounded(shape=(*batch_size, self.B - 1), dtype=self.float_type),
            agg_pol_location=Unbounded(shape=(*batch_size, self.n_block_locations), dtype=self.float_type),
            agg_pod_location=Unbounded(shape=(*batch_size, self.n_block_locations), dtype=self.float_type),
            timestep=Unbounded(shape=(*batch_size, 1), dtype=th.int64),
            action_mask=Bounded(shape=(*batch_size, self.n_block_locations), low=0, high=1, dtype=th.bool),
            excess_pod_locations=Unbounded(shape=(*batch_size, self.B * self.BL), dtype=self.float_type),
            total_profit=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),
            max_total_profit=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),
            shape=batch_size,
        )
        self.observation_spec = Composite(
            # State, action, generator
            observation=state_spec,
            action=Unbounded(shape=(*batch_size, self.n_block_locations), dtype=self.float_type),

            # Performance
            profit=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),
            revenue=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),
            cost=Unbounded(shape=(*batch_size, 1), dtype=torch.float32),

            # Constraints
            clip_min=Unbounded(shape=(*batch_size,self.n_block_locations),dtype=self.float_type),
            clip_max=Unbounded(shape=(*batch_size,self.n_block_locations),dtype=self.float_type),
            lhs_A=Unbounded(shape=(*batch_size,self.n_constraints,self.n_block_locations),dtype=self.float_type),
            rhs=Unbounded(shape=(*batch_size,self.n_constraints),dtype=self.float_type),
            violation=Unbounded(shape=(*batch_size,self.n_constraints),dtype=self.float_type),
            shape=batch_size,
        )
        self.action_spec = Bounded(
            shape=(*batch_size, self.n_block_locations),  # Define shape as needed
            low=0.0,
            high=50.0,  # Define high value as needed
            dtype=self.float_type,
        )
        self.reward_spec = Unbounded(shape=(*batch_size,1,))
        self.done_spec = Unbounded(shape=(*batch_size,1,), dtype=th.bool)

    def _extract_from_block_td(self, td:TensorDict, batch_size:Tuple) -> Tuple:
        """Extract action, reward and step from the TensorDict."""
        # Environment-related parameters
        timestep = td["observation", "timestep"].view(-1).clone()

        # Action-related variables
        action = {
            "action": td["action"].view(*batch_size, self.B, self.D, self.BL).clone(),
            "action_mask": td["observation", "action_mask"].view(*batch_size, self.B, self.D, self.BL).clone(),
            "lhs_A": td["lhs_A"].clone(),
            "rhs": td["rhs"].clone(),
            "clip_max": td["clip_max"].view(*batch_size, self.B, self.D, self.BL).clone(),
            "clip_min": td["clip_min"].view(*batch_size, -1).clone(),
        }

        # Demand-related variables
        demand = {
            # clones are needed to prevent in-place
            "expected_demand": td["observation", "expected_demand"].view(*batch_size, self.T, self.K).clone(),
            "std_demand": td["observation", "std_demand"].view(*batch_size, self.T, self.K).clone(),
            "realized_demand": td["observation", "realized_demand"].view(*batch_size, self.T, self.K).clone(),
            "observed_demand": td["observation", "observed_demand"].view(*batch_size, self.T, self.K).clone(),
            "current_demand": td["observation", "realized_demand"].clone()[..., timestep[0]].view(*batch_size, 1),
        }
        # Vessel-related variables
        vessel = {
        "utilization": td["observation", "utilization"].view(*batch_size, self.B, self.D, self.BL, self.T, self.K).clone(),
        "target_long_crane": td["observation", "target_long_crane"].view(*batch_size, 1).clone(),
        "long_crane_moves_discharge":td["observation", "long_crane_moves_discharge"].view(*batch_size, self.B-1).clone(),
        "residual_capacity": td["observation", "residual_capacity"].view(*batch_size, self.B, self.D, self.BL).clone(),
        }
        return action, demand, vessel, timestep

    # Constraints
    def _compact_form_block_shapes(self, ) -> None:
        """Define shapes for compact form"""
        self.n_demand = 1
        self.n_block_locations = self.B * self.D * self.BL
        self.n_stability = 4
        self.n_block_stow = self.n_block_locations + 1
        self.n_constraints = self.n_demand + self.n_block_locations + self.n_stability #+ self.n_block_stow

    def _create_constraint_matrix_block(self, shape: Tuple[int, int, int, int], rhs:bool=True) -> Tensor:
        """Create constraint matrix A for compact constraints Au <= b"""
        # [1, LM-TW, TW-LM, VM-TW, TW-VM]
        A = th.ones(shape, device=self.device, dtype=self.float_type)
        scaling = self.teus.view(1, 1, 1, -1) if rhs else 1
        A[self.n_demand:self.n_block_locations + self.n_demand,] *= scaling * th.eye(self.n_block_locations, device=self.device, dtype=self.float_type).view(self.n_block_locations, self.n_block_locations, 1, 1)
        A *= self.block_constraint_signs.view(-1, 1, 1, 1)
        A[self.n_block_locations + self.n_demand:self.n_block_locations + self.n_demand + self.n_stability] *= self.block_stability_params_lhs.view(self.n_stability, self.n_block_locations, 1, self.K,)
        return A.view(self.n_constraints, self.n_block_locations, -1)

    # Initialize
    def _initialize_block_capacity(self, capacity:Tensor) -> None:
        """Initialize capacity parameters for block environment"""
        self.capacity = th.zeros((self.B, self.D, self.BL), device=self.device, dtype=self.float_type)
        block_ratios = [2 / (self.BL + 1)] + [1 / (self.BL + 1)] * (self.BL - 1)
        for i, ratio in enumerate(block_ratios):
            self.capacity[..., i] = th.full((self.B, self.D), capacity * ratio, device=self.device, dtype=self.float_type)
        # Needed for 2 paired-blocks; i.e., 3 hatch covers
        if self.BL == 2:
            self.capacity[...,0] = th.ceil(self.capacity[...,0]/2)*2
            self.capacity[...,1] = th.floor(self.capacity[...,1]/2)*2

    def _initialize_block_stability(self, ) -> None:
        """Initialize stability parameters"""
        self.block_stability_params_lhs = self._precompute_block_stability_parameters()

    def _initialize_block_constraints(self, ) -> None:
        """Initialize constraint-related parameters."""
        self.block_constraint_signs = th.ones(self.n_constraints, device=self.device, dtype=self.float_type)
        self.block_constraint_signs[th.tensor([-3, -1], device=self.device)] *= -1  # Flip signs for specific constraints

        # Swap signs for stability constraints, only the first one remains positive
        self.swap_signs_block_stability = -th.ones_like(self.block_constraint_signs)
        self.swap_signs_block_stability[0] = 1

        # Create constraint matrix
        self.block_A_rhs = self._create_constraint_matrix_block(shape=(self.n_constraints, self.n_block_locations, self.T, self.K), rhs=True)
        self.block_A_lhs = self._create_constraint_matrix_block(shape=(self.n_constraints, self.n_block_locations, self.T, self.K), rhs=False)

    # Precomputes
    def _precompute_block_stability_parameters(self,) -> Tensor:
        """Precompute lhs block stability parameters for compact constraints. Get rhs by negating lhs."""
        lp_weight = self.lp_weight.view(-1, self.B, 1, 1, self.K).expand(-1,-1,self.D,self.BL,-1)
        vp_weight = self.vp_weight.view(-1, 1, self.D, 1, self.K).expand(-1,self.B,-1,self.BL,-1)
        p_weight = th.cat([lp_weight, lp_weight, vp_weight, vp_weight], dim=0)
        target = torch.tensor([self.LCG_target, self.LCG_target, self.VCG_target, self.VCG_target],
                              device=self.device, dtype=self.float_type).view(-1,1,1,1,1)
        delta = torch.tensor([self.stab_delta, -self.stab_delta, self.stab_delta, -self.stab_delta],
                             device=self.device, dtype=self.float_type).view(-1,1,1,1,1)
        output = p_weight - self.weights.view(1,1,1,1,self.K) * (target + delta)
        return output.view(-1, self.n_block_locations, self.K,)