from __future__ import annotations

import unittest

from analysis_service import AnalysisService
from ctm import CTMSimulator
from dynamic_analysis import DynamicAnalysisService
from models import Link, Movement, Network, Node, Project, Route, SimulationConfig, Source
from network_dynamic import Cell, DynamicLink, DynamicNetwork
from network_migration import ensure_dynamic_schema
from project_loader import ProjectLoader


class DynamicPipelineTests(unittest.TestCase):
    def test_loader_infers_dynamic_entities_for_existing_project(self):
        project = ProjectLoader().load("network_project.json")
        self.assertTrue(project.network.sources)
        self.assertTrue(project.network.sinks)
        self.assertTrue(project.network.movements)
        self.assertIn("migration_diagnostics", project.metadata)

    def test_compare_mode_populates_dynamic_results_and_comparison(self):
        project = self._build_simple_project()
        report = AnalysisService().analyze_project(project, mode="compare")

        self.assertEqual(report["Project_Name"], project.project_name)
        self.assertIn("comparison", report)
        self.assertTrue(report["Links_Analysis"])

        link_result = report["Links_Analysis"][0]
        self.assertIn("avg_flow_pcu_h", link_result)
        self.assertIn("comparison", link_result)
        self.assertGreaterEqual(link_result["avg_flow_pcu_h"], 0.0)
        self.assertLessEqual(link_result["avg_flow_pcu_h"], link_result["C_initial"] + 1e-6)

    def test_route_inference_prioritizes_routes_over_topology(self):
        network = Network()
        for node_id, node_type in (("N1", "source"), ("N2", "intersection"), ("N3", "sink"), ("N4", "sink")):
            network.add_node(Node(id=node_id, name=node_id, node_type=node_type))

        network.add_link(
            Link(
                id="L1",
                name="Entry",
                start_node_id="N1",
                end_node_id="N2",
                length_km=0.2,
                traffic_counts={"car": 900},
                parameters={"lanes_total": 1, "capacity_per_lane_base": 1800},
            )
        )
        network.add_link(
            Link(
                id="L2",
                name="Preferred Exit",
                start_node_id="N2",
                end_node_id="N3",
                length_km=0.2,
                parameters={"lanes_total": 1, "capacity_per_lane_base": 1800},
            )
        )
        network.add_link(
            Link(
                id="L3",
                name="Alt Exit",
                start_node_id="N2",
                end_node_id="N4",
                length_km=0.2,
                parameters={"lanes_total": 1, "capacity_per_lane_base": 1800},
            )
        )
        network.add_route(Route(id="R1", name="main", link_ids=["L1", "L2"]))
        project = Project(project_name="route-priority", network=network)

        ensure_dynamic_schema(project)
        outgoing = [
            movement
            for movement in project.network.movements.values()
            if movement.from_link_id == "L1"
        ]
        ratios = {movement.to_link_id: movement.split_ratio for movement in outgoing}
        self.assertGreater(ratios["L2"], ratios["L3"])

    def test_link_level_capacity_parameter_changes_result(self):
        baseline_project = self._build_simple_project()
        for source in baseline_project.network.sources.values():
            if source.link_id == "L1":
                source.demand_by_type["car"] = 900
        baseline_result = AnalysisService().analyze_project(baseline_project, mode="dynamic")["Links_Analysis"][0]

        project = self._build_simple_project()
        project.network.links["L1"].parameters["capacity_per_lane_base"] = 500
        for source in project.network.sources.values():
            if source.link_id == "L1":
                source.demand_by_type["car"] = 900
        report = AnalysisService().analyze_project(project, mode="dynamic")
        link_result = report["Links_Analysis"][0]
        self.assertLessEqual(link_result["C_initial"], 500)
        self.assertGreater(link_result["VC_ratio"], baseline_result["VC_ratio"])

    def test_node_split_uses_original_sending_for_each_movement(self):
        network = DynamicNetwork(
            nodes={},
            links={
                "L1": self._dynamic_link("L1", "N1", "N2", occupancy=1.0),
                "L2": self._dynamic_link("L2", "N2", "N3"),
                "L3": self._dynamic_link("L3", "N2", "N4"),
            },
            sources={},
            sinks={},
            movements={
                "M1": Movement(id="M1", node_id="N2", from_link_id="L1", to_link_id="L2", split_ratio=0.5),
                "M2": Movement(id="M2", node_id="N2", from_link_id="L1", to_link_id="L3", split_ratio=0.5),
            },
        )

        flows = CTMSimulator(network, SimulationConfig())._compute_node_flows(0)
        self.assertAlmostEqual(flows["M1"], 0.5, places=6)
        self.assertAlmostEqual(flows["M2"], 0.5, places=6)

    def test_migration_backfills_missing_entities_in_mixed_schema(self):
        network = Network()
        for node_id, node_type in (
            ("N1", "source"),
            ("N2", "intersection"),
            ("N3", "sink"),
            ("N4", "source"),
            ("N5", "intersection"),
            ("N6", "sink"),
        ):
            network.add_node(Node(id=node_id, name=node_id, node_type=node_type))
        for link_id, start, end in (
            ("L1", "N1", "N2"),
            ("L2", "N2", "N3"),
            ("L3", "N4", "N5"),
            ("L4", "N5", "N6"),
        ):
            network.add_link(
                Link(
                    id=link_id,
                    name=link_id,
                    start_node_id=start,
                    end_node_id=end,
                    length_km=0.1,
                    traffic_counts={"car": 300} if link_id in {"L1", "L3"} else {},
                    parameters={"lanes_total": 1, "capacity_per_lane_base": 1800},
                )
            )
        network.add_source(Source(id="SRC_L1", link_id="L1", demand_by_type={"car": 300}))
        network.add_movement(Movement(id="M_N2_L1_L2", node_id="N2", from_link_id="L1", to_link_id="L2"))

        project = Project(project_name="mixed", network=network)
        ensure_dynamic_schema(project)

        self.assertIn("L3", {source.link_id for source in project.network.sources.values()})
        self.assertIn("L4", {sink.link_id for sink in project.network.sinks.values()})
        self.assertIn("L3", {movement.from_link_id for movement in project.network.movements.values()})

    def test_link_overrides_take_precedence_over_link_parameters(self):
        project = self._build_simple_project()
        project.simulation.link_overrides["L1"] = {"capacity_per_lane_base": 500}
        result = AnalysisService().analyze_project(project, mode="dynamic")["Links_Analysis"][0]
        self.assertEqual(result["C_initial"], 500)

    def test_adaptive_dt_keeps_short_link_cell_length_physical(self):
        project = self._build_simple_project()
        project.network.links["L1"].length_km = 0.02
        project.simulation.dt_seconds = 5
        runtime_network = DynamicAnalysisService()._build_runtime_network(project)
        link = runtime_network.links["L1"]
        self.assertLessEqual(link.cell_length_m, link.length_m)
        self.assertEqual(link.dt_seconds, 1)

    def _build_simple_project(self) -> Project:
        network = Network()
        network.add_node(Node(id="N1", name="N1", node_type="source"))
        network.add_node(Node(id="N2", name="N2", node_type="sink"))
        network.add_link(
            Link(
                id="L1",
                name="Main Link",
                start_node_id="N1",
                end_node_id="N2",
                link_type="straight",
                length_km=0.3,
                traffic_counts={"car": 600},
                parameters={"lanes_total": 1, "capacity_per_lane_base": 1800},
            )
        )
        project = Project(
            project_name="simple",
            pcu_coefficients={"car": 1.0},
            network=network,
            simulation=SimulationConfig(horizon_seconds=300, dt_seconds=1),
        )
        ensure_dynamic_schema(project)
        return project

    def _dynamic_link(self, link_id: str, start_node_id: str, end_node_id: str, occupancy: float = 0.0) -> DynamicLink:
        return DynamicLink(
            id=link_id,
            name=link_id,
            start_node_id=start_node_id,
            end_node_id=end_node_id,
            link_type="straight",
            length_m=100.0,
            lanes=1.0,
            dt_seconds=1,
            free_flow_speed_kph=60.0,
            wave_speed_kph=20.0,
            jam_density_pcu_per_km_lane=150.0,
            capacity_pcu_h=3600.0,
            cell_length_m=100.0,
            parameters={},
            metadata={},
            cells=[Cell(occupancy_pcu=occupancy)],
        )


if __name__ == "__main__":
    unittest.main()
