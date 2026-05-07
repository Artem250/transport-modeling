from __future__ import annotations

import unittest

from analysis_service import AnalysisService
from demand_assignment_service import DemandAssignmentService
from models import Link, Network, Node, Project, Route, Scenario
from scenario_service import ScenarioService
from validation_service import ValidationService


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
            project.network.links["L_W_IN"].metadata["observed_traffic_counts"],
            {"car": 999},
        )
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["routes"]), 2)

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
        self.assertEqual(report["routes"][0]["link_ids"], ["AB", "BC"])
        self.assertEqual(project.network.routes["R"].link_ids, ["AB", "BC"])
        self.assertEqual(project.network.routes["R"].metadata["assigned_link_ids"], ["AB", "BC"])
        self.assertEqual(project.network.links["AB"].traffic_counts["car"], 500)
        self.assertEqual(project.network.links["BC"].traffic_counts["car"], 500)

    def test_assign_calculates_demand_from_route_split_coefficients(self):
        project = build_project()
        project.demand_model = {
            "type": "route_split_coefficients",
            "boundary_flows": {"B_WEST": 1200},
            "route_split_coefficients": [
                {
                    "id": "RS_WE",
                    "from": "B_WEST",
                    "to": "B_EAST",
                    "coefficient": 0.7,
                    "link_ids": ["L_W_IN", "L_CENTER"],
                },
                {
                    "id": "RS_WS",
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

    def test_assignment_rejects_disconnected_route_links(self):
        project = build_project()
        project.demand_model = {
            "type": "routes",
            "boundary_flows": {"B_WEST": 1200},
            "routes": [
                {
                    "id": "BAD",
                    "demand_veh_h": 100,
                    "link_ids": ["L_CENTER", "L_W_IN"],
                }
            ],
        }

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["assigned_routes"], 0)
        self.assertTrue(any("discontinuity" in error for error in report["errors"]))
        self.assertEqual(project.network.links["L_CENTER"].traffic_counts, {})
        self.assertTrue(any("actually assigned 0.0" in warning for warning in report["warnings"]))

    def test_assignment_requires_mode_when_multiple_sources_are_present(self):
        project = build_project()
        project.demand_model["route_split_coefficients"] = [
            {
                "id": "RS_WE",
                "from": "B_WEST",
                "to": "B_EAST",
                "coefficient": 1.0,
                "link_ids": ["L_W_IN", "L_CENTER"],
            }
        ]

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["assigned_routes"], 0)
        self.assertTrue(any("Ambiguous demand model" in error for error in report["errors"]))

    def test_assignment_rejects_origin_destination_mismatch_and_disabled_links(self):
        project = build_project()
        project.network.links["L_CENTER"].metadata["disabled"] = True
        project.demand_model = {
            "type": "routes",
            "routes": [
                {
                    "id": "BAD",
                    "origin_node_id": "B_WEST",
                    "destination_node_id": "B_SOUTH",
                    "demand_value": 100,
                    "link_ids": ["L_CENTER"],
                }
            ],
        }

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["assigned_routes"], 0)
        self.assertTrue(any("disabled link" in error for error in report["errors"]))
        self.assertTrue(any("expected origin B_WEST" in error for error in report["errors"]))
        self.assertTrue(any("expected destination B_SOUTH" in error for error in report["errors"]))

    def test_assignment_rejects_route_split_demand_override(self):
        project = build_project()
        project.demand_model = {
            "type": "route_split_coefficients",
            "boundary_flows": {"B_WEST": 1200},
            "route_split_coefficients": [
                {
                    "id": "RS_BAD",
                    "from": "B_WEST",
                    "to": "B_EAST",
                    "coefficient": 1.0,
                    "demand_value": 1200,
                    "link_ids": ["L_W_IN", "L_CENTER"],
                }
            ],
        }

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["assigned_routes"], 0)
        self.assertTrue(any("do not set demand_value" in error for error in report["errors"]))

    def test_assignment_propagates_node_turning_ratios(self):
        project = build_project()
        project.demand_model = {
            "type": "node_turning_ratios",
            "boundary_flows": {"B_WEST": 1200},
            "node_turning_ratios": [
                {
                    "id": "T_EAST",
                    "node_id": "I1",
                    "from_link_id": "L_W_IN",
                    "to_link_id": "L_CENTER",
                    "share": 0.7,
                },
                {
                    "id": "T_SOUTH",
                    "node_id": "I1",
                    "from_link_id": "L_W_IN",
                    "to_link_id": "L_SOUTH",
                    "share": 0.3,
                },
            ],
        }

        report = DemandAssignmentService().assign(project)

        self.assertEqual(report["errors"], [])
        self.assertEqual(report["demand_model_type"], "node_turning_ratios")
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts["car"], 1200)
        self.assertEqual(project.network.links["L_CENTER"].traffic_counts["car"], 840)
        self.assertEqual(project.network.links["L_SOUTH"].traffic_counts["car"], 360)
        self.assertEqual(report["assigned_links"], 3)

    def test_analysis_runs_assignment_before_link_analysis(self):
        project = build_project()

        report = AnalysisService().analyze_project(project)

        self.assertEqual(report["Demand_Assignment"]["assigned_routes"], 2)
        self.assertEqual(project.network.links["L_W_IN"].results["V"], 1200)
        self.assertEqual(len(report["Demand_Routes_Analysis"]), 2)

    def test_analysis_skips_link_analysis_when_assignment_fails(self):
        project = build_project()
        project.demand_model["route_split_coefficients"] = [
            {
                "id": "RS_WE",
                "from": "B_WEST",
                "to": "B_EAST",
                "coefficient": 1.0,
                "link_ids": ["L_W_IN", "L_CENTER"],
            }
        ]

        report = AnalysisService().analyze_project(project)

        self.assertIn("failed", report["Analysis_Status"])
        self.assertEqual(report["Links_Analysis"], [])
        self.assertEqual(project.network.links["L_W_IN"].results, {})

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

    def test_scenario_can_update_route_split_coefficient(self):
        project = build_project()
        project.demand_model = {
            "route_split_coefficients": [{"id": "RS_WE", "coefficient": 0.7}],
        }
        scenario = Scenario(
            id="turn",
            name="Turn",
            changes=[
                {
                    "type": "update_route_split_coefficient",
                    "movement_id": "RS_WE",
                    "coefficient": 0.8,
                }
            ],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        self.assertEqual(
            scenario_project.demand_model["route_split_coefficients"][0]["coefficient"],
            0.8,
        )

    def test_validation_checks_demand_model(self):
        project = build_project()
        project.demand_model = {
            "type": "route_split_coefficients",
            "unit": "bad-unit",
            "boundary_flows": {"B_WEST": -1},
            "route_split_coefficients": [
                {
                    "id": "RS_BAD",
                    "from": "B_WEST",
                    "coefficient": -0.1,
                    "demand_veh_h": 100,
                    "link_ids": ["missing"],
                }
            ],
        }

        errors = ValidationService().validate_project(project)

        self.assertTrue(any("unit" in error for error in errors))
        self.assertTrue(any("volume cannot be negative" in error for error in errors))
        self.assertTrue(any("coefficient cannot be negative" in error for error in errors))
        self.assertTrue(any("missing link" in error for error in errors))

    def test_validation_checks_node_turning_ratios(self):
        project = build_project()
        project.demand_model = {
            "type": "node_turning_ratios",
            "boundary_flows": {"MISSING": 100},
            "node_turning_ratios": [
                {
                    "id": "BAD_TURN",
                    "node_id": "B_EAST",
                    "from_link_id": "L_W_IN",
                    "to_link_id": "L_SOUTH",
                    "share": -0.2,
                }
            ],
        }

        errors = ValidationService().validate_project(project)

        self.assertTrue(any("node is missing" in error for error in errors))
        self.assertTrue(any("share cannot be negative" in error for error in errors))
        self.assertTrue(any("node_id B_EAST does not match" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
