from __future__ import annotations

import unittest

from demand_assignment_service import DemandAssignmentService
from models import Link, Network, Node, Project, Scenario
from road_sections import Intersection, StraightRoad
from scenario_service import ScenarioService
from validation_service import ValidationService


def build_project(demand_model: dict | None = None) -> Project:
    network = Network()
    for node_id in ("B_WEST", "B_NORTH", "I1", "B_EAST", "B_SOUTH"):
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
            results={"LOS": "OLD"},
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
        project_name="Edge case test",
        pcu_coefficients={"car": 1.0, "pcu": 1.0},
        network=network,
        demand_model=demand_model or {},
    )


def split_model(policy: str | None = None) -> dict:
    model = {
        "type": "route_split_coefficients",
        "unit": "veh/h",
        "boundary_flows": {"B_WEST": 1200},
        "route_split_coefficients": [
            {
                "id": "RS_WE",
                "from": "B_WEST",
                "to": "B_EAST",
                "coefficient": 0.7,
                "vehicle_type": "car",
                "link_ids": ["L_W_IN", "L_CENTER"],
            },
            {
                "id": "RS_WS",
                "from": "B_WEST",
                "to": "B_SOUTH",
                "coefficient": 0.3,
                "vehicle_type": "car",
                "link_ids": ["L_W_IN", "L_SOUTH"],
            },
        ],
    }
    if policy:
        model["split_balance_policy"] = policy
    return model


class DemandAssignmentEdgeCasesTest(unittest.TestCase):
    def test_boundary_flow_without_route_split_is_error_by_default(self):
        model = split_model()
        model["boundary_flows"]["B_NORTH"] = 500
        project = build_project(model)

        validation_errors = ValidationService().validate_project(project)
        assignment_report = DemandAssignmentService().assign(project)

        self.assertTrue(any("Boundary node B_NORTH" in error for error in validation_errors))
        self.assertFalse(assignment_report["success"])
        self.assertTrue(any("Boundary node B_NORTH" in error for error in assignment_report["errors"]))
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts, {"car": 999})
        self.assertEqual(project.network.links["L_W_IN"].results, {"LOS": "OLD"})

    def test_boundary_flow_without_route_split_can_be_explicitly_unassigned(self):
        model = split_model(policy="allow_unassigned")
        model["boundary_flows"]["B_NORTH"] = 500
        project = build_project(model)

        report = DemandAssignmentService().assign(project)

        self.assertTrue(report["success"])
        self.assertTrue(any("B_NORTH" in warning for warning in report["warnings"]))
        self.assertEqual(report["boundary_flow_summary"]["B_NORTH"]["assigned_flow"], 0.0)
        self.assertEqual(report["boundary_flow_summary"]["B_NORTH"]["unassigned_flow"], 500.0)

    def test_zero_coefficient_does_not_create_zero_traffic_counts(self):
        model = split_model(policy="allow_unassigned")
        model["route_split_coefficients"][1]["coefficient"] = 0.0
        project = build_project(model)

        report = DemandAssignmentService().assign(project)

        self.assertTrue(report["success"])
        self.assertEqual(report["assigned_routes"], 1)
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts, {"car": 840.0})
        self.assertEqual(project.network.links["L_CENTER"].traffic_counts, {"car": 840.0})
        self.assertEqual(project.network.links["L_SOUTH"].traffic_counts, {})
        self.assertNotEqual(
            project.network.links["L_SOUTH"].metadata.get("traffic_counts_source"),
            "assigned_demand",
        )

    def test_scenario_numeric_fields_are_normalized_to_float(self):
        project = build_project(split_model())
        scenario = Scenario(
            id="numeric",
            name="Numeric",
            changes=[
                {"type": "update_boundary_flow", "boundary_id": "B_WEST", "volume": "1500"},
                {
                    "type": "update_route_split_coefficient",
                    "movement_id": "RS_WE",
                    "coefficient": "0.6",
                },
            ],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        self.assertEqual(scenario_project.demand_model["boundary_flows"]["B_WEST"], 1500.0)
        self.assertEqual(
            scenario_project.demand_model["route_split_coefficients"][0]["coefficient"],
            0.6,
        )

    def test_intersection_capacity_handles_zero_cycle_time(self):
        section = Intersection(
            section_id="I_ZERO",
            name="Zero cycle",
            traffic_counts={"car": 100},
            pcu_coeffs={"car": 1.0},
            length_km=0.1,
            cycle_time=0,
            green_time=30,
            saturation_flow_base=1800,
            lanes_count=1,
            lane_width_m=3.5,
            grade_percent=0,
            parking_present=False,
            heavy_vehicles_percent=0,
        )

        section.analyze_performance()
        optimization = section.optimize()

        self.assertEqual(section.C, 0.0)
        self.assertEqual(section.los, "F")
        self.assertIsNotNone(optimization)
        self.assertIn("CRITICAL", optimization["proposal"])

    def test_straight_road_handles_no_effective_lanes(self):
        section = StraightRoad(
            section_id="L_ZERO",
            name="No lanes",
            traffic_counts={"car": 100},
            pcu_coeffs={"car": 1.0},
            length_km=0.1,
            lanes_total=1,
            lanes_bus=1,
            capacity_per_lane_base=1800,
            lane_width_m=3.5,
            grade_percent=0,
            parking_present=False,
            heavy_vehicles_percent=0,
        )

        section.analyze_performance()
        optimization = section.optimize()

        self.assertEqual(section.C, 0.0)
        self.assertEqual(section.los, "F")
        self.assertIsNotNone(optimization)
        self.assertIn("capacity is zero", optimization["proposal"])


if __name__ == "__main__":
    unittest.main()
