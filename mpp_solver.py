import pulp as pl
import json
from dataclasses import dataclass
from typing import List, Dict

@dataclass
class Container:
    id: int
    pol: int
    pod: int
    teu: int
    weight: float
    revenue: float

@dataclass
class Bay:
    id: int
    capacity_teu: int
    capacity_weight: float

@dataclass
class MPPResult:
    assignment: Dict[int, int]  # container_id -> bay_id
    objective: float


def load_instance(path: str):
    with open(path) as f:
        data = json.load(f)
    bays = [Bay(**b) for b in data["bays"]]
    containers = [Container(**c) for c in data["containers"]]
    return bays, containers


def solve_mpp(bays: List[Bay], containers: List[Container]) -> MPPResult:
    prob = pl.LpProblem("mpp", pl.LpMaximize)
    x = pl.LpVariable.dicts("x", ((c.id, b.id) for c in containers for b in bays), 0, 1, pl.LpBinary)
    # Each container assigned once
    for c in containers:
        prob += pl.lpSum(x[(c.id, b.id)] for b in bays) == 1
    # Capacity
    for b in bays:
        prob += pl.lpSum(c.teu * x[(c.id, b.id)] for c in containers) <= b.capacity_teu
        prob += pl.lpSum(c.weight * x[(c.id, b.id)] for c in containers) <= b.capacity_weight
    prob += pl.lpSum(c.revenue * x[(c.id, b.id)] for c in containers for b in bays)
    prob.solve(pl.PULP_CBC_CMD(msg=False))
    assignment = {c.id: next(b.id for b in bays if pl.value(x[(c.id, b.id)]) > 0.5) for c in containers}
    return MPPResult(assignment, pl.value(prob.objective))

if __name__ == "__main__":
    bays, containers = load_instance("benchmark/toy_instance.json")
    res = solve_mpp(bays, containers)
    print(json.dumps({"assignment": res.assignment, "obj": res.objective}, indent=2))
