from __future__ import annotations

import copy
import unittest

from analysis_service import AnalysisService
from demand_assignment_service import DemandAssignmentService
from models import Link, Network, Node, Project, Route, Scenario
from project_saver import ProjectSaver
from scenario_service import ScenarioService
from validation_service import ValidationService


def build_project(demand_model: dict | None = None) -> Project:
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
            results={"LOS": "OLD"},
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
            results={"LOS": "OLD"},
        )
    )

    return Project(
        project_name="Demand test",
        pcu_coefficients={"car": 1.0, "pcu": 1.0},
        network=network,
        demand_model=demand_model or {},
    )


def routes_model(unit: str = "veh/h") -> dict:
    return {
        "type": "routes",
        "unit": unit,
        "routes": [
            {
                "id": "R_WE",
                "origin_node_id": "B_WEST",
                "destination_node_id": "B_EAST",
                "demand_value": 800,
                "vehicle_type": "car",
                "link_ids": ["L_W_IN", "L_CENTER"],
            },
            {
                "id": "R_WS",
                "origin_node_id": "B_WEST",
                "destination_node_id": "B_SOUTH",
                "demand_value": 400,
                "vehicle_type": "car",
                "link_ids": ["L_W_IN", "L_SOUTH"],
            },
        ],
    }


def split_model(
    coefficient_a: float = 0.7,
    coefficient_b: float = 0.3,
    policy: str | None = None,
) -> dict:
    model = {
        "type": "route_split_coefficients",
        "unit": "veh/h",
        "boundary_flows": {"B_WEST": 1200},
        "route_split_coefficients": [
            {
                "id": "RS_WE",
                "from": "B_WEST",
                "to": "B_EAST",
                "coefficient": coefficient_a,
                "vehicle_type": "car",
                "link_ids": ["L_W_IN", "L_CENTER"],
            },
            {
                "id": "RS_WS",
                "from": "B_WEST",
                "to": "B_SOUTH",
                "coefficient": coefficient_b,
                "vehicle_type": "car",
                "link_ids": ["L_W_IN", "L_SOUTH"],
            },
        ],
    }
    if policy is not None:
        model["split_balance_policy"] = policy
    return model


class FailingAssignmentService:
    def assign(self, project: Project) -> dict:
        return {
            "success": False,
            "demand_model_type": project.demand_model.get("type"),
            "unit": project.demand_model.get("unit", "veh/h"),
            "assigned_routes": 0,
            "routes": [],
            "link_assignments": {},
            "warnings": [],
            "errors": ["synthetic assignment failure"],
        }


class DemandAssignmentServiceTest(unittest.TestCase):
    def test_routes_mode_sums_shared_input_and_branches(self):
        project = build_project(routes_model())

        report = DemandAssignmentService().assign(project)

        self.assertTrue(report["success"])
        self.assertEqual(report["assigned_routes"], 2)
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts["car"], 1200)
        self.assertEqual(project.network.links["L_CENTER"].traffic_counts["car"], 800)
        self.assertEqual(project.network.links["L_SOUTH"].traffic_counts["car"], 400)
        self.assertEqual(
            project.network.links["L_W_IN"].metadata["observed_traffic_counts"],
            {"car": 999},
        )

    def test_route_split_coefficients_mode_assigns_boundary_flow(self):
        project = build_project(split_model())

        report = DemandAssignmentService().assign(project)

        self.assertTrue(report["success"])
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts["car"], 1200)
        self.assertEqual(project.network.links["L_CENTER"].traffic_counts["car"], 840)
        self.assertEqual(project.network.links["L_SOUTH"].traffic_counts["car"], 360)

    def test_assignment_is_transactional_when_route_has_missing_link(self):
        project = build_project(routes_model())
        project.demand_model["routes"][1]["link_ids"] = ["L_W_IN", "MISSING"]
        original_counts = {
            link_id: copy.deepcopy(link.traffic_counts)
            for link_id, link in project.network.links.items()
        }
        original_results = {
            link_id: copy.deepcopy(link.results)
            for link_id, link in project.network.links.items()
        }

        report = DemandAssignmentService().assign(project)

        self.assertFalse(report["success"])
        self.assertTrue(any("missing link MISSING" in error for error in report["errors"]))
        self.assertEqual(
            {link_id: link.traffic_counts for link_id, link in project.network.links.items()},
            original_counts,
        )
        self.assertEqual(
            {link_id: link.results for link_id, link in project.network.links.items()},
            original_results,
        )

    def test_route_path_validation_errors(self):
        cases = [
            ("disconnected", {"link_ids": ["L_CENTER", "L_W_IN"]}, "disconnected"),
            ("origin", {"origin_node_id": "B_EAST"}, "expected origin B_EAST"),
            ("destination", {"destination_node_id": "B_EAST", "link_ids": ["L_W_IN", "L_SOUTH"]}, "expected destination B_EAST"),
            ("disabled", {"disabled_link": "L_CENTER"}, "disabled link"),
        ]

        for _, override, expected in cases:
            project = build_project(routes_model())
            route = project.demand_model["routes"][0]
            route.update({key: value for key, value in override.items() if key != "disabled_link"})
            if "disabled_link" in override:
                project.network.links[override["disabled_link"]].metadata["disabled"] = True

            report = DemandAssignmentService().assign(project)

            self.assertFalse(report["success"])
            self.assertTrue(
                any(expected in error for error in report["errors"]),
                report["errors"],
            )

    def test_route_split_validation_errors_and_allow_unassigned_warning(self):
        invalid_cases = [
            (split_model(-0.1, 1.1), "cannot be negative"),
            (split_model(0.7, 1.1), "greater than 1"),
            (split_model(0.8, 0.5), "greater than 1"),
            (split_model(0.7, 0.0), "less than 1"),
        ]
        for model, expected in invalid_cases:
            project = build_project(model)
            report = DemandAssignmentService().assign(project)
            self.assertFalse(report["success"])
            self.assertTrue(any(expected in error for error in report["errors"]), report["errors"])

        project = build_project(split_model(0.7, 0.0, policy="allow_unassigned"))
        report = DemandAssignmentService().assign(project)

        self.assertTrue(report["success"])
        self.assertTrue(any("less than 1" in warning for warning in report["warnings"]))
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts["car"], 840)

    def test_route_split_rejects_embedded_demand_value(self):
        model = split_model()
        model["route_split_coefficients"][0]["demand_value"] = 840
        project = build_project(model)

        report = DemandAssignmentService().assign(project)

        self.assertFalse(report["success"])
        self.assertTrue(any("remove demand_value" in error for error in report["errors"]))

    def test_pcu_unit_writes_pcu_counts_without_vehicle_type(self):
        model = routes_model(unit="pcu/h")
        model["routes"] = [model["routes"][0]]
        model["routes"][0]["demand_value"] = 500
        model["routes"][0]["vehicle_type"] = "car"
        project = build_project(model)

        report = DemandAssignmentService().assign(project)

        self.assertTrue(report["success"])
        self.assertEqual(project.network.links["L_W_IN"].traffic_counts, {"pcu": 500})
        self.assertNotIn("car", project.network.links["L_W_IN"].traffic_counts)

    def test_analysis_validation_failed_skips_link_analysis(self):
        project = build_project(routes_model())
        project.demand_model["routes"][0]["link_ids"] = ["MISSING"]

        report = AnalysisService().analyze_project(project)

        self.assertEqual(report["Analysis_Status"], "Validation failed")
        self.assertEqual(report["Links_Analysis"], [])
        self.assertEqual(project.network.links["L_W_IN"].results["LOS"], "UNDEFINED")
        self.assertEqual(
            project.network.links["L_W_IN"].results["Analysis_Status"],
            "Validation failed",
        )

    def test_analysis_assignment_failed_skips_link_analysis(self):
        project = build_project(routes_model())
        service = AnalysisService()
        service.demand_assignment_service = FailingAssignmentService()

        report = service.analyze_project(project)

        self.assertEqual(report["Analysis_Status"], "Demand assignment failed")
        self.assertEqual(report["Links_Analysis"], [])
        self.assertEqual(project.network.links["L_W_IN"].results["LOS"], "UNDEFINED")
        self.assertEqual(
            project.network.links["L_W_IN"].results["Analysis_Status"],
            "Demand assignment failed",
        )

    def test_analysis_successful_assignment_fills_link_and_demand_route_reports(self):
        project = build_project(routes_model())

        report = AnalysisService().analyze_project(project)

        self.assertEqual(report["Analysis_Status"], "OK")
        self.assertTrue(report["Links_Analysis"])
        self.assertEqual(len(report["Demand_Routes_Analysis"]), 2)
        self.assertEqual(project.network.links["L_W_IN"].results["V"], 1200)

    def test_scenario_scales_routes_demand_value(self):
        project = build_project(routes_model())
        scenario = Scenario(
            id="growth",
            name="Growth",
            changes=[{"type": "scale_all_route_demand", "factor": 1.2}],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        routes = scenario_project.demand_model["routes"]
        self.assertEqual(routes[0]["demand_value"], 960)
        self.assertEqual(routes[1]["demand_value"], 480)

    def test_scenario_scales_boundary_flows_not_split_coefficients(self):
        project = build_project(split_model())
        scenario = Scenario(
            id="growth",
            name="Growth",
            changes=[{"type": "scale_all_route_demand", "factor": 1.2}],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        self.assertEqual(scenario_project.demand_model["boundary_flows"]["B_WEST"], 1440)
        coefficients = [
            split["coefficient"]
            for split in scenario_project.demand_model["route_split_coefficients"]
        ]
        self.assertEqual(coefficients, [0.7, 0.3])

    def test_scenario_updates_route_split_coefficient_only(self):
        project = build_project(split_model(0.7, 0.2))
        scenario = Scenario(
            id="split",
            name="Split",
            changes=[
                {
                    "type": "update_route_split_coefficient",
                    "movement_id": "RS_WE",
                    "coefficient": 0.8,
                }
            ],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        split = scenario_project.demand_model["route_split_coefficients"][0]
        self.assertEqual(split["coefficient"], 0.8)
        self.assertNotIn("demand_value", split)
        self.assertEqual(ValidationService().validate_project(scenario_project), [])

    def test_scenario_update_traffic_with_demand_model_adds_warning(self):
        project = build_project(routes_model())
        scenario = Scenario(
            id="traffic",
            name="Traffic",
            changes=[
                {
                    "type": "update_traffic",
                    "link_id": "L_W_IN",
                    "traffic_counts": {"car": 123},
                }
            ],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        self.assertEqual(scenario_project.network.links["L_W_IN"].traffic_counts, {"car": 999})
        self.assertTrue(scenario_project.metadata["scenario_warnings"])

    def test_scenario_warns_when_change_is_ignored(self):
        project = build_project(routes_model())
        scenario = Scenario(
            id="bad",
            name="Bad",
            changes=[{"type": "update_route_demand", "route_id": "MISSING", "demand_value": 100}],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        self.assertTrue(
            any("route_id MISSING not found" in warning for warning in scenario_project.metadata["scenario_warnings"])
        )

    def test_scenario_scales_string_boundary_flow_as_number(self):
        project = build_project(split_model())
        project.demand_model["boundary_flows"]["B_WEST"] = "1200"
        scenario = Scenario(
            id="growth",
            name="Growth",
            changes=[{"type": "scale_all_route_demand", "factor": 1.2}],
        )

        scenario_project = ScenarioService().apply_scenario(project, scenario)

        self.assertEqual(scenario_project.demand_model["boundary_flows"]["B_WEST"], 1440)

    def test_route_split_allow_unassigned_reports_unassigned_flow(self):
        project = build_project(split_model(0.7, 0.0, policy="allow_unassigned"))

        report = DemandAssignmentService().assign(project)

        summary = report["boundary_flow_summary"]["B_WEST"]
        self.assertEqual(summary["assigned_flow"], 840)
        self.assertEqual(summary["unassigned_flow"], 360)

    def test_routes_mode_warns_when_boundary_flow_balance_differs(self):
        model = routes_model()
        model["boundary_flows"] = {"B_WEST": 1200}
        model["routes"] = [model["routes"][0]]
        project = build_project(model)

        report = DemandAssignmentService().assign(project)

        self.assertTrue(report["success"])
        self.assertTrue(any("boundary_flow=1200.0" in warning for warning in report["warnings"]))

    def test_project_saver_serializes_visual_routes_without_demand_value(self):
        project = build_project()
        project.network.add_route(
            Route(
                id="VISUAL",
                name="Visual route",
                link_ids=["L_W_IN"],
                demand_value=500,
            )
        )

        serialized = ProjectSaver()._serialize_route(project.network.routes["VISUAL"])

        self.assertNotIn("demand_value", serialized)


if __name__ == "__main__":
    unittest.main()
