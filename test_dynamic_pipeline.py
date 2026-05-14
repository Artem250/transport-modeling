from __future__ import annotations

import csv
import unittest
from pathlib import Path

from analysis_service import AnalysisService
from ctm import CTMSimulator
from dynamic_analysis import DynamicAnalysisService
from models import Link, Movement, Network, Node, Project, Route, SimulationConfig, Sink, Source
from network_dynamic import Cell, DynamicLink, DynamicNetwork
from network_migration import ensure_dynamic_schema
from project_loader import ProjectLoader
from skdf_matcher import load_skdf_roads
from skdf_segment_project import build_project_from_segments_csv


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

    def test_receiving_capacity_is_limited_by_capacity_step(self):
        link = self._dynamic_link("L1", "N1", "N2")
        link.wave_speed_kph = 200.0
        link.capacity_pcu_h = 3600.0

        self.assertAlmostEqual(link.receiving_capacity(0), 1.0, places=6)

    def test_virtual_detector_metrics_are_reported(self):
        project = self._build_simple_project()
        project.network.links["L1"].observed_counts = {"car": 1200}

        report = AnalysisService().analyze_project(project, mode="dynamic")
        detector_metrics = report["Diagnostics"]["virtual_detectors"]

        self.assertEqual(detector_metrics["detector_count"], 1)
        self.assertIn("rmse_pcu_h", detector_metrics)
        self.assertIn("mape_pct", detector_metrics)
        self.assertIn("bias_pcu_h", detector_metrics)
        self.assertEqual(detector_metrics["status"], "review")

    def test_bottleneck_creates_upstream_queue(self):
        bottleneck = self._build_two_link_project(upstream_capacity=2000, downstream_capacity=1000, demand=1800)
        free_downstream = self._build_two_link_project(upstream_capacity=1000, downstream_capacity=2000, demand=900)

        bottleneck_result = AnalysisService().analyze_project(bottleneck, mode="dynamic")["Links_Analysis"][0]
        free_result = AnalysisService().analyze_project(free_downstream, mode="dynamic")["Links_Analysis"][0]

        self.assertGreater(bottleneck_result["max_queue_pcu"], free_result["max_queue_pcu"])
        self.assertGreater(bottleneck_result["queue_length_m"], 0.0)

    def test_square_reroutes_around_blocked_direction(self):
        project = self._build_square_project(blocked_capacity=0)
        report = AnalysisService().analyze_project(project, mode="dynamic")
        links = {item["id"]: item for item in report["Links_Analysis"]}

        self.assertEqual(links["L_FAST"]["throughput_pcu"], 0.0)
        self.assertGreater(links["L_DETOUR"]["throughput_pcu"], 0.0)

    def test_node_balance_preserves_mass_without_boundaries(self):
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
                "M1": Movement(id="M1", node_id="N2", from_link_id="L1", to_link_id="L2", split_ratio=0.4),
                "M2": Movement(id="M2", node_id="N2", from_link_id="L1", to_link_id="L3", split_ratio=0.6),
            },
        )
        before = sum(link.total_occupancy() for link in network.links.values())

        CTMSimulator(network, SimulationConfig()).simulate(1)

        after = sum(link.total_occupancy() for link in network.links.values())
        self.assertAlmostEqual(after, before, places=6)

    def test_skdf_matcher_prefers_segment_fields(self):
        csv_path = self._write_segment_csv()

        roads = load_skdf_roads(csv_path)

        self.assertEqual(len(roads), 1)
        self.assertEqual(roads[0].segment_object_id, "SEG42")
        self.assertEqual(roads[0].road_name, "Segment Road")
        self.assertEqual(roads[0].traffic, 777)
        self.assertEqual(roads[0].capacity, 2222)
        self.assertEqual(roads[0].lanes, 3)
        self.assertEqual(roads[0].speed_limit, 50)

    def test_skdf_segment_project_builds_links_from_segment_csv(self):
        csv_path = self._write_segment_csv()

        project_data = build_project_from_segments_csv(csv_path)
        link = project_data["network"]["links"][0]

        self.assertEqual(link["link_type"], "skdf_segment")
        self.assertEqual(link["parameters"]["capacity_total_skdf"], 2222)
        self.assertEqual(link["parameters"]["speed_limit_skdf"], 50)
        self.assertEqual(link["observed_counts"]["car"], 777)
        self.assertEqual(link["metadata"]["skdf"]["segment_object_id"], "SEG42")

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

    def _build_two_link_project(self, upstream_capacity: float, downstream_capacity: float, demand: float) -> Project:
        network = Network()
        network.add_node(Node(id="N1", name="N1", node_type="source"))
        network.add_node(Node(id="N2", name="N2", node_type="intersection"))
        network.add_node(Node(id="N3", name="N3", node_type="sink"))
        network.add_link(self._skdf_link("L1", "Upstream", "N1", "N2", upstream_capacity, observed=demand))
        network.add_link(self._skdf_link("L2", "Downstream", "N2", "N3", downstream_capacity, observed=demand))
        network.add_source(Source(id="SRC_L1", link_id="L1", demand_by_type={"car": demand}))
        network.add_sink(Sink(id="SNK_L2", link_id="L2"))
        network.add_movement(Movement(id="M_N2_L1_L2", node_id="N2", from_link_id="L1", to_link_id="L2"))
        return Project(
            project_name="bottleneck",
            pcu_coefficients={"car": 1.0},
            network=network,
            simulation=SimulationConfig(horizon_seconds=600, dt_seconds=1, target_cell_length_m=50.0),
        )

    def _build_square_project(self, blocked_capacity: float) -> Project:
        network = Network()
        for node_id, node_type in (
            ("N1", "source"),
            ("N2", "intersection"),
            ("N3", "sink"),
            ("N4", "sink"),
        ):
            network.add_node(Node(id=node_id, name=node_id, node_type=node_type))

        network.add_link(self._skdf_link("L_IN", "Entry", "N1", "N2", 2000, observed=1200))
        network.add_link(self._skdf_link("L_FAST", "Blocked", "N2", "N3", blocked_capacity, observed=900))
        network.add_link(self._skdf_link("L_DETOUR", "Detour", "N2", "N4", 2000, observed=100))
        network.add_source(Source(id="SRC_L_IN", link_id="L_IN", demand_by_type={"car": 1200}))
        network.add_sink(Sink(id="SNK_FAST", link_id="L_FAST"))
        network.add_sink(Sink(id="SNK_DETOUR", link_id="L_DETOUR"))
        network.add_movement(Movement(id="M_FAST", node_id="N2", from_link_id="L_IN", to_link_id="L_FAST", split_ratio=0.5))
        network.add_movement(Movement(id="M_DETOUR", node_id="N2", from_link_id="L_IN", to_link_id="L_DETOUR", split_ratio=0.5))
        return Project(
            project_name="square",
            pcu_coefficients={"car": 1.0},
            network=network,
            simulation=SimulationConfig(
                horizon_seconds=300,
                dt_seconds=1,
                target_cell_length_m=50.0,
                split_update_interval_s=30,
                split_inertia_alpha=0.25,
            ),
        )

    def _skdf_link(
        self,
        link_id: str,
        name: str,
        start_node_id: str,
        end_node_id: str,
        capacity: float,
        observed: float,
    ) -> Link:
        return Link(
            id=link_id,
            name=name,
            start_node_id=start_node_id,
            end_node_id=end_node_id,
            length_km=0.2,
            observed_counts={"car": observed},
            parameters={
                "lanes_total": 1,
                "capacity_total_skdf": capacity,
                "speed_limit_skdf": 60,
            },
            metadata={
                "skdf": {
                    "traffic": observed,
                    "capacity_total": capacity,
                    "lanes": 1,
                    "speed_limit": 60,
                    "directional": True,
                }
            },
        )

    def _write_segment_csv(self) -> Path:
        csv_path = Path.cwd() / f"_segment_test_{id(self)}.csv"
        self.addCleanup(lambda: csv_path.exists() and csv_path.unlink())
        fieldnames = [
            "geometry_segment",
            "geometry",
            "road_id",
            "road_part_id",
            "road_part_id_segment",
            "segment_object_id",
            "segment_feature_id",
            "road_name",
            "road_name_segment",
            "full_name",
            "start_km_segment",
            "finish_km_segment",
            "traffic_segment",
            "traffic_1",
            "capacity_segment",
            "capacity_1",
            "lanes_segment",
            "lanes_1",
            "top_speed_segment",
            "speed_limit_1",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(
                {
                    "geometry_segment": "[[[0, 0], [1000, 0]]]",
                    "geometry": "[[[0, 0], [10, 0]]]",
                    "road_id": "R1",
                    "road_part_id": "RP_ROAD",
                    "road_part_id_segment": "RP_SEG",
                    "segment_object_id": "SEG42",
                    "segment_feature_id": "FEAT42",
                    "road_name": "Road Level",
                    "road_name_segment": "Segment Road",
                    "full_name": "Segment Road Full",
                    "start_km_segment": "1.5",
                    "finish_km_segment": "2.0",
                    "traffic_segment": "777",
                    "traffic_1": "111",
                    "capacity_segment": "2222",
                    "capacity_1": "333",
                    "lanes_segment": "3",
                    "lanes_1": "1",
                    "top_speed_segment": "50",
                    "speed_limit_1": "20",
                }
            )
        return csv_path

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
