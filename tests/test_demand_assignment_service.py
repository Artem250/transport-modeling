from __future__ import annotations

import unittest

from analysis_service import AnalysisService
from demand_assignment_service import DemandAssignmentService
from models import Link, Network, Node, Project, Route, Scenario
from scenario_service import ScenarioService


def build_project() -> Project:
    network = Network()
    for node_id in ("B_WEST", "I1", "B_EAST", "B_SOUTH"):
        network.add_node(Node(id=node_id))

    network.add_link(
        Link(
            id="L_W_IN",
            name="West input",
            start_node_id="B_WEST",
            end_node_id="I1",
            length_km=0.4,
            traffic_counts={"car": 999},
            parameters={"lanes_total": 2, "capacity_per_lane_base": 1800},
        )
    )
    network.add_link(
        Link(
            id="L_CENTER",
            name="Center",
            start_node_id="I1",
            end_node_id="B_EAST",
            length_km=0.7,
            parameters={"lanes_total": 2, "capacity_per_lane_base": 1800},
        )
    )
    network.add_link(
        Link(
            id="L_SOUTH",
            name="South",
            start_node_id="I1",
            end_node_id="B_SOUTH",
            length_km=0.5,
            parameters={"lanes_total": 1, "capacity_per_lane_base": 1500},
        )
    )

    return Project(
        project_name="Demand test",
        pcu_coefficients={"car": 1.0},
        network=network,
        demand_model={
            "boundary_flows": {"B_WEST": 1200},
            "routes": [
                {
                    "id": "R_WE",
                    "origin_node_id": "B_WEST",
                    "destination_node_id": "B_EAST",
                    "demand_veh_h": 800,
                    "vehicle_type": "car",
                    "link_ids": ["L_W_IN", "L_CENTER"],
                },
                {
                    "id": "R_WS",
                    "from": "B_WEST",
                    "to": "B_SOUTH",
                    "demand_veh_h": 400,
                    "vehicle_type": "car",
                    "link_ids": ["L_W_IN", "L_SOUTH"],
                },
            ],
        },
    )


class DemandAssignmentServiceTest(unittest.TestCase):
    def test_assign_sums_route_demand_on_links(self):
        project = build_project()

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["assigned_routes"], 2)
        self.assertEqual(report["warnings"], [])
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts["car"], 1200)
        self.assertEqual(project.network.links["L_CENTER"].traffic_counts["car"], 800)
        self.assertEqual(project.network.links["L_SOUTH"].traffic_counts["car"], 400)
        self.assertEqual(
            project.network.links["L_W_IN"].metadata["source_traffic_counts"],
            {"car": 999},
        )

    def test_assign_builds_shortest_path_when_route_has_no_links(self):
        project = Project(network=Network())
        for node_id in ("A", "B", "C"):
            project.network.add_node(Node(id=node_id))
        project.network.add_link(Link(id="AB", name="AB", start_node_id="A", end_node_id="B", length_km=1))
        project.network.add_link(Link(id="BC", name="BC", start_node_id="B", end_node_id="C", length_km=1))
        project.network.routes["R"] = Route(
            id="R",
            name="A-C",
            origin_node_id="A",
            destination_node_id="C",
            demand_veh_h=500,
        )

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["assigned_routes"], 1)
        self.assertEqual(project.network.links["AB"].traffic_counts["car"], 500)
        self.assertEqual(project.network.links["BC"].traffic_counts["car"], 500)

    def test_assign_calculates_demand_from_turning_coefficients(self):
        project = build_project()
        project.demand_model = {
            "boundary_flows": {"B_WEST": 1200},
            "turning_coefficients": [
                {
                    "id": "TC_WE",
                    "from": "B_WEST",
                    "to": "B_EAST",
                    "coefficient": 0.7,
                    "link_ids": ["L_W_IN", "L_CENTER"],
                },
                {
                    "id": "TC_WS",
                    "from": "B_WEST",
                    "to": "B_SOUTH",
                    "coefficient": 0.3,
                    "link_ids": ["L_W_IN", "L_SOUTH"],
                },
            ],
        }

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["warnings"], [])
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts["car"], 1200)
        self.assertEqual(project.network.links["L_CENTER"].traffic_counts["car"], 840)
        self.assertEqual(project.network.links["L_SOUTH"].traffic_counts["car"], 360)

    def test_analysis_runs_assignment_before_link_analysis(self):
        project = build_project()

        report = AnalysisService().analyze_project(project)

        self.assertEqual(report["Demand_Assignment"]["assigned_routes"], 2)
        self.assertEqual(project.network.links["L_W_IN"].results["V"], 1200)

    def test_scenario_can_scale_route_demand(self):
        project = build_project()
        scenario = Scenario(
            id="growth",
            name="Growth",
            changes=[{"type": "scale_all_route_demand", "factor": 1.2}],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        routes = scenario_project.demand_model["routes"]
        self.assertEqual(routes[0]["demand_veh_h"], 960)
        self.assertEqual(routes[1]["demand_veh_h"], 480)
        self.assertEqual(scenario_project.demand_model["boundary_flows"]["B_WEST"], 1440)

    def test_scenario_can_update_turning_coefficient(self):
        project = build_project()
        project.demand_model = {
            "turning_coefficients": [{"id": "TC_WE", "coefficient": 0.7}],
        }
        scenario = Scenario(
            id="turn",
            name="Turn",
            changes=[
                {
                    "type": "update_turning_coefficient",
                    "movement_id": "TC_WE",
                    "coefficient": 0.8,
                }
            ],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        self.assertEqual(
            scenario_project.demand_model["turning_coefficients"][0]["coefficient"],
            0.8,
        )


if __name__ == "__main__":
    unittest.main()
