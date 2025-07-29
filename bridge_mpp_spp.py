import json
from typing import Dict, List

import mpp_solver
from mpp_solver import Container, Bay, MPPResult


def bridge_to_spp(res: MPPResult, containers: List[Container], bays: List[Bay]) -> Dict[int, List[int]]:
    """Create SPP input from MPP result."""
    mapping: Dict[int, List[int]] = {b.id: [] for b in bays}
    for c in containers:
        b_id = res.assignment[c.id]
        mapping[b_id].append(c.id)
    return mapping

if __name__ == "__main__":
    bays, containers = mpp_solver.load_instance("benchmark/toy_instance.json")
    res = mpp_solver.solve_mpp(bays, containers)
    mapping = bridge_to_spp(res, containers, bays)
    print(json.dumps(mapping, indent=2))
