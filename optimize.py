import json
import mpp_solver
from bridge_mpp_spp import bridge_to_spp
import spp_solver


def main():
    bays, containers = mpp_solver.load_instance("benchmark/toy_instance.json")
    mpp_res = mpp_solver.solve_mpp(bays, containers)
    mapping = bridge_to_spp(mpp_res, containers, bays)
    spp_res = spp_solver.solve_spp(mapping, containers, bays, slots_per_bay=25)
    result = {
        "mpp_objective": mpp_res.objective,
        "spp_objective": spp_res.objective,
        "mpp_assignment": mpp_res.assignment,
        "spp_assignment": spp_res.assignment,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
