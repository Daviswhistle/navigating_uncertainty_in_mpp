from dataclasses import dataclass
from typing import Dict, List
import pulp as pl

from mpp_solver import Container, Bay
import mpp_solver

@dataclass
class SPPSolution:
    assignment: Dict[int, Dict[int, int]]
    objective: float


def solve_spp(mapping: Dict[int, List[int]], containers: List[Container], bays: List[Bay], slots_per_bay: int) -> SPPSolution:
    prob = pl.LpProblem("spp", pl.LpMaximize)
    cont_lookup = {c.id: c for c in containers}
    y = pl.LpVariable.dicts("y", ((b.id, s, c_id) for b in bays for s in range(slots_per_bay) for c_id in mapping[b.id]), 0, 1, pl.LpBinary)
    for b in bays:
        for s in range(slots_per_bay):
            prob += pl.lpSum(y[(b.id, s, c_id)] for c_id in mapping[b.id]) <= 1
    for b in bays:
        for c_id in mapping[b.id]:
            prob += pl.lpSum(y[(b.id, s, c_id)] for s in range(slots_per_bay)) == 1
    for b in bays:
        prob += pl.lpSum(cont_lookup[c_id].teu * y[(b.id, s, c_id)] for c_id in mapping[b.id] for s in range(slots_per_bay)) <= b.capacity_teu
        prob += pl.lpSum(cont_lookup[c_id].weight * y[(b.id, s, c_id)] for c_id in mapping[b.id] for s in range(slots_per_bay)) <= b.capacity_weight
    prob += pl.lpSum(cont_lookup[c_id].revenue * y[(b.id, s, c_id)] for b in bays for s in range(slots_per_bay) for c_id in mapping[b.id])
    prob.solve(pl.PULP_CBC_CMD(msg=False))
    assignment: Dict[int, Dict[int, int]] = {b.id: {} for b in bays}
    for b in bays:
        for s in range(slots_per_bay):
            for c_id in mapping[b.id]:
                if pl.value(y[(b.id, s, c_id)]) > 0.5:
                    assignment[b.id][s] = c_id
    return SPPSolution(assignment, pl.value(prob.objective))

if __name__ == "__main__":
    from bridge_mpp_spp import bridge_to_spp
    bays, containers = mpp_solver.load_instance("benchmark/toy_instance.json")
    mpp_res = mpp_solver.solve_mpp(bays, containers)
    mapping = bridge_to_spp(mpp_res, containers, bays)
    res = solve_spp(mapping, containers, bays, slots_per_bay=25)
    print(res.assignment)
