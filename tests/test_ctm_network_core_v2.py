import unittest

from ctm_network_core import (
    CTMModel,
    CTMStateError,
    Incident,
    TriangularFundamentalDiagram,
)
from ctm_experiment_runner import apply_lane_changes
from ctm_simulator import CTMScenarioConfig, CTMSimulator
from ctm_simulator import (
    CTMScenarioConfig as TheoryCTMScenarioConfig,
    CTMSimulator as TheoryCTMSimulator,
)
from models import Link, Network, Node, Project
from project_loader import ProjectLoader


def diagram():
    return TriangularFundamentalDiagram.from_common_units(
        free_flow_speed_kph=60.0,
        backward_wave_speed_kph=18.0,
        capacity_pcu_h=1800.0,
        jam_density_pcu_km=150.0,
    )


def movement_test_project(metadata=None) -> Project:
    network = Network()
    for node in (
        Node(id="A", lon=82.0, lat=55.0, node_type="boundary"),
        Node(id="N", lon=82.001, lat=55.0, node_type="intersection"),
        Node(id="B", lon=82.002, lat=55.0, node_type="boundary"),
        Node(id="C", lon=82.001, lat=55.001, node_type="boundary"),
        Node(id="D", lon=82.001, lat=54.999, node_type="boundary"),
    ):
        network.add_node(node)

    common_counts = {"car": 600}
    common_parameters = {"lanes_total": 1}
    links = [
        Link(
            id="L_IN",
            name="Main",
            start_node_id="A",
            end_node_id="N",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters=common_parameters,
            coords={"type": "polyline", "points": [[82.0, 55.0], [82.001, 55.0]]},
            metadata={
                "highway": "residential",
                "osm_way_id": "10",
                "osm_name": "Main",
                "osm_direction": "forward",
                "osm_is_oneway": True,
            },
        ),
        Link(
            id="L_STRAIGHT",
            name="Main",
            start_node_id="N",
            end_node_id="B",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters=common_parameters,
            coords={"type": "polyline", "points": [[82.001, 55.0], [82.002, 55.0]]},
            metadata={
                "highway": "residential",
                "osm_way_id": "10",
                "osm_name": "Main",
                "osm_direction": "forward",
                "osm_is_oneway": True,
            },
        ),
        Link(
            id="L_LEFT",
            name="Side",
            start_node_id="N",
            end_node_id="C",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters=common_parameters,
            coords={"type": "polyline", "points": [[82.001, 55.0], [82.001, 55.001]]},
            metadata={
                "highway": "residential",
                "osm_way_id": "20",
                "osm_name": "Side",
                "osm_direction": "forward",
                "osm_is_oneway": True,
            },
        ),
        Link(
            id="L_RIGHT",
            name="Side",
            start_node_id="N",
            end_node_id="D",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters=common_parameters,
            coords={"type": "polyline", "points": [[82.001, 55.0], [82.001, 54.999]]},
            metadata={
                "highway": "residential",
                "osm_way_id": "30",
                "osm_name": "Side",
                "osm_direction": "forward",
                "osm_is_oneway": True,
            },
        ),
        Link(
            id="L_UTURN",
            name="Main",
            start_node_id="N",
            end_node_id="A",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters=common_parameters,
            coords={"type": "polyline", "points": [[82.001, 55.0], [82.0, 55.0]]},
            metadata={
                "highway": "residential",
                "osm_way_id": "10",
                "osm_name": "Main",
                "osm_direction": "reverse",
                "osm_is_oneway": True,
            },
        ),
    ]
    for link in links:
        network.add_link(link)

    return Project(
        project_name="movement test",
        pcu_coefficients={"car": 1.0},
        network=network,
        metadata=metadata or {},
    )


def source_inflow_test_project() -> Project:
    network = Network()
    for node in (
        Node(id="S1", lon=82.0, lat=55.0, node_type="boundary"),
        Node(id="S2", lon=82.0, lat=55.001, node_type="boundary"),
        Node(id="N", lon=82.001, lat=55.0, node_type="intersection"),
        Node(id="T", lon=82.002, lat=55.0, node_type="boundary"),
    ):
        network.add_node(node)

    common_counts = {"car": 600}
    links = [
        Link(
            id="L_SRC_FAST",
            name="Fast source",
            start_node_id="S1",
            end_node_id="N",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters={"lanes_total": 1},
            coords={"type": "polyline", "points": [[82.0, 55.0], [82.001, 55.0]]},
            metadata={"highway": "primary", "osm_direction": "forward", "osm_is_oneway": True},
        ),
        Link(
            id="L_SRC_SLOW",
            name="Slow source",
            start_node_id="S2",
            end_node_id="N",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters={"lanes_total": 1},
            coords={"type": "polyline", "points": [[82.0, 55.001], [82.001, 55.0]]},
            metadata={"highway": "residential", "osm_direction": "forward", "osm_is_oneway": True},
        ),
        Link(
            id="L_OUT",
            name="Out",
            start_node_id="N",
            end_node_id="T",
            length_km=0.12,
            traffic_counts=common_counts,
            parameters={"lanes_total": 2},
            coords={"type": "polyline", "points": [[82.001, 55.0], [82.002, 55.0]]},
            metadata={"highway": "primary", "osm_direction": "forward", "osm_is_oneway": True},
        ),
    ]
    for link in links:
        network.add_link(link)

    return Project(
        project_name="source inflow test",
        pcu_coefficients={"car": 1.0},
        network=network,
    )


class CTMNetworkCoreV2Test(unittest.TestCase):
    def test_step_with_boundary_flows_conserves_mass(self):
        model = CTMModel.create_uniform_link(
            length=100.0,
            cell_length=50.0,
            diagram=diagram(),
            dt=1.0,
            initial_density=20.0 / 1000.0,
        )
        before = model.total_occupancy()

        diagnostics = model.step_with_boundary_flows(
            upstream_flow=0.2,
            downstream_flow=0.1,
        )

        expected_total = before + 0.1
        self.assertAlmostEqual(model.total_occupancy(), expected_total, places=9)
        self.assertAlmostEqual(diagnostics["conservation_error_pcu"], 0.0, places=9)

    def test_rejects_upstream_flow_above_first_cell_supply(self):
        model = CTMModel.create_uniform_link(
            length=100.0,
            cell_length=50.0,
            diagram=diagram(),
            dt=1.0,
        )

        with self.assertRaises(CTMStateError):
            model.step_with_boundary_flows(
                upstream_flow=0.6,
                downstream_flow=0.0,
            )

    def test_rejects_downstream_flow_above_last_cell_demand(self):
        model = CTMModel.create_uniform_link(
            length=100.0,
            cell_length=50.0,
            diagram=diagram(),
            dt=1.0,
        )

        with self.assertRaises(CTMStateError):
            model.step_with_boundary_flows(
                upstream_flow=0.0,
                downstream_flow=0.1,
            )

    def test_incident_reduces_internal_flow_and_builds_upstream_density(self):
        model = CTMModel.create_uniform_link(
            length=300.0,
            cell_length=100.0,
            diagram=diagram(),
            dt=1.0,
            initial_density=30.0 / 1000.0,
            incidents=[
                Incident(
                    cell_index=1,
                    start_time=0.0,
                    end_time=60.0,
                    capacity_factor=0.1,
                )
            ],
        )
        before_density = model.cells[0].density

        model.step_with_boundary_flows(
            upstream_flow=0.2,
            downstream_flow=0.1,
        )

        self.assertGreater(model.cells[0].density, before_density)
        self.assertLessEqual(max(model.densities()), model.diagram.jam_density)


class CTMMovementTableTest(unittest.TestCase):
    def test_default_config_is_saved_to_metadata(self):
        project = movement_test_project()
        simulator = CTMSimulator(project)

        self.assertIn("ctm_scenario_config", project.metadata)
        self.assertEqual(
            project.metadata["ctm_scenario_config"]["simulation_minutes"],
            simulator.config.simulation_minutes,
        )
        self.assertEqual(
            project.metadata["node_solver"],
            "explicit_diverge_merge_general_ctm_node_solver",
        )

    def test_polyline_turn_angle_uses_segments_near_intersection(self):
        project = movement_test_project()
        project.network.links["L_IN"].coords = {
            "type": "polyline",
            "points": [[82.0, 55.0], [82.001, 54.999], [82.001, 55.0]],
        }
        simulator = CTMSimulator(project)

        angle = simulator._calc_turn_angle(
            project.network.links["L_IN"],
            project.network.links["L_STRAIGHT"],
        )

        self.assertAlmostEqual(angle, -90.0, delta=1.0)

    def test_inferred_movements_prefer_same_road_and_block_u_turn(self):
        project = movement_test_project()
        simulator = CTMSimulator(project)

        movements = {
            movement["out_link_id"]: movement
            for movement in simulator.movements_by_node["N"]["L_IN"]
        }

        self.assertIn("L_STRAIGHT", movements)
        self.assertIn("L_LEFT", movements)
        self.assertIn("L_RIGHT", movements)
        self.assertNotIn("L_UTURN", movements)
        self.assertGreater(
            movements["L_STRAIGHT"]["turn_ratio"],
            movements["L_LEFT"]["turn_ratio"],
        )
        self.assertGreater(
            movements["L_STRAIGHT"]["turn_ratio"],
            movements["L_RIGHT"]["turn_ratio"],
        )
        self.assertIn("same_osm_way_id", movements["L_STRAIGHT"]["flags"])
        self.assertAlmostEqual(
            sum(movement["turn_ratio"] for movement in movements.values()),
            1.0,
            places=6,
        )

    def test_same_road_bonuses_are_read_from_config(self):
        default_project = movement_test_project()
        default_simulator = CTMSimulator(default_project)
        default_ratio = next(
            movement["turn_ratio"]
            for movement in default_simulator.movements_by_node["N"]["L_IN"]
            if movement["out_link_id"] == "L_STRAIGHT"
        )

        weak_bonus_project = movement_test_project()
        weak_bonuses = dict(CTMScenarioConfig().same_road_bonuses)
        weak_bonuses["same_osm_way_id"] = 1.0
        weak_bonuses["same_osm_name"] = 1.0
        weak_bonuses["same_visible_name"] = 1.0
        weak_simulator = CTMSimulator(
            weak_bonus_project,
            CTMScenarioConfig(same_road_bonuses=weak_bonuses),
        )
        weak_ratio = next(
            movement["turn_ratio"]
            for movement in weak_simulator.movements_by_node["N"]["L_IN"]
            if movement["out_link_id"] == "L_STRAIGHT"
        )

        self.assertLess(weak_ratio, default_ratio)
        self.assertEqual(
            weak_bonus_project.metadata["ctm_scenario_config"]["same_road_bonuses"]["same_osm_way_id"],
            1.0,
        )

    def test_movement_lane_weight_uses_base_lanes_when_scenario_changes_capacity(self):
        base_project = movement_test_project()
        base_simulator = CTMSimulator(base_project)
        base_left_ratio = next(
            movement["turn_ratio"]
            for movement in base_simulator.movements_by_node["N"]["L_IN"]
            if movement["out_link_id"] == "L_LEFT"
        )

        scenario_project = movement_test_project()
        scenario_project.network.links["L_LEFT"].parameters = {
            "lanes_total": 3,
            "lanes_total_base": 1,
        }
        scenario_simulator = CTMSimulator(scenario_project)
        scenario_left_ratio = next(
            movement["turn_ratio"]
            for movement in scenario_simulator.movements_by_node["N"]["L_IN"]
            if movement["out_link_id"] == "L_LEFT"
        )

        direct_project = movement_test_project()
        direct_project.network.links["L_LEFT"].parameters = {"lanes_total": 3}
        direct_simulator = CTMSimulator(direct_project)
        direct_left_ratio = next(
            movement["turn_ratio"]
            for movement in direct_simulator.movements_by_node["N"]["L_IN"]
            if movement["out_link_id"] == "L_LEFT"
        )

        self.assertAlmostEqual(scenario_left_ratio, base_left_ratio)
        self.assertGreater(direct_left_ratio, scenario_left_ratio)

    def test_movement_summary_contains_solver_and_ratio_diagnostics(self):
        project = movement_test_project()
        CTMSimulator(project)

        summary = project.metadata["ctm_movement_summary"]

        self.assertEqual(
            summary["node_solver"],
            "explicit_diverge_merge_general_ctm_node_solver",
        )
        self.assertEqual(summary["fifo_strength"], 0.0)
        self.assertGreater(summary["movement_count"], 0)
        self.assertGreaterEqual(summary["max_turn_ratio"], 0.0)
        self.assertIn("turn_ratio_gt_0_9_count", summary)
        self.assertIn("turn_ratio_gt_0_95_count", summary)

    def test_manual_override_replaces_inferred_ratios(self):
        project = movement_test_project(
            metadata={
                "turn_ratio_overrides": {
                    "N": {
                        "L_IN": {
                            "L_STRAIGHT": 0.7,
                            "L_LEFT": 0.2,
                            "L_RIGHT": 0.1,
                        }
                    }
                }
            }
        )
        simulator = CTMSimulator(project)

        movements = {
            movement["out_link_id"]: movement
            for movement in simulator.movements_by_node["N"]["L_IN"]
        }

        self.assertEqual(set(movements), {"L_STRAIGHT", "L_LEFT", "L_RIGHT"})
        self.assertEqual(movements["L_STRAIGHT"]["source"], "manual")
        self.assertIn("manual_override", movements["L_STRAIGHT"]["flags"])
        self.assertAlmostEqual(movements["L_STRAIGHT"]["turn_ratio"], 0.7)
        self.assertAlmostEqual(movements["L_LEFT"]["turn_ratio"], 0.2)
        self.assertAlmostEqual(movements["L_RIGHT"]["turn_ratio"], 0.1)

    def test_manual_override_rejects_invalid_sum(self):
        project = movement_test_project(
            metadata={
                "turn_ratio_overrides": {
                    "N": {
                        "L_IN": {
                            "L_STRAIGHT": 0.7,
                            "L_LEFT": 0.2,
                        }
                    }
                }
            }
        )

        with self.assertRaises(CTMStateError):
            CTMSimulator(project)

    def test_manual_override_rejects_ratio_outside_unit_interval(self):
        project = movement_test_project(
            metadata={
                "turn_ratio_overrides": {
                    "N": {
                        "L_IN": {
                            "L_STRAIGHT": 1.1,
                            "L_LEFT": -0.1,
                            "L_RIGHT": 0.0,
                        }
                    }
                }
            }
        )

        with self.assertRaises(CTMStateError):
            CTMSimulator(project)

    def test_manual_override_rejects_non_outgoing_link(self):
        project = movement_test_project(
            metadata={
                "turn_ratio_overrides": {
                    "N": {
                        "L_IN": {
                            "L_STRAIGHT": 0.7,
                            "L_IN": 0.3,
                        }
                    }
                }
            }
        )

        with self.assertRaises(CTMStateError):
            CTMSimulator(project)

    def test_configured_incident_metadata_uses_actual_link(self):
        project = movement_test_project()
        CTMSimulator(project, CTMScenarioConfig(incident_link_id="L_IN"))

        self.assertEqual(project.metadata["ctm_incident"]["link_id"], "L_IN")
        self.assertIn("incident", project.network.links["L_IN"].results)


class CTMScenarioConfigTest(unittest.TestCase):
    def test_rejects_invalid_scalar_config_values(self):
        invalid_kwargs = [
            {"dt_seconds": 0.0},
            {"simulation_minutes": 0},
            {"snapshot_interval_sec": 0},
            {"cell_length_target_m": 0.0},
            {"inflow_pcu_per_hour": -1.0},
            {"source_inflow_allocation": "unknown"},
            {"source_inflows_pcu_per_hour": {"L1": -1.0}},
            {"jam_density_pcu_km_per_lane": 0.0},
            {"incident_start_sec": 10.0, "incident_end_sec": 10.0},
            {"incident_capacity_factor": -0.1},
            {"incident_capacity_factor": 1.1},
            {"incident_speed_factor": -0.1},
            {"fifo_strength": -0.1},
            {"fifo_strength": 1.1},
        ]

        for kwargs in invalid_kwargs:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    CTMScenarioConfig(**kwargs)

    def test_rejects_invalid_highway_params(self):
        params = dict(CTMScenarioConfig().highway_params)
        params["default"] = {"speed_kph": 40, "cap_per_lane": 600}

        with self.assertRaises(ValueError):
            CTMScenarioConfig(highway_params=params)


class CTMTheoryIncidentTest(unittest.TestCase):
    def test_direct_capacity_factor_is_used_when_blocked_lanes_is_none(self):
        project = movement_test_project()
        TheoryCTMSimulator(
            project,
            TheoryCTMScenarioConfig(
                incident_link_id="L_IN",
                incident_capacity_factor=0.25,
                incident_blocked_lanes=None,
            ),
        )

        incident = project.metadata["ctm_incident"]
        self.assertEqual(incident["incident_model"], "direct_capacity_factor")
        self.assertAlmostEqual(incident["capacity_factor"], 0.25)
        self.assertEqual(incident["configured_capacity_factor"], 0.25)

    def test_lane_blockage_capacity_factor_uses_open_lane_share(self):
        project = movement_test_project()
        project.network.links["L_IN"].parameters = {"lanes_total": 2}

        TheoryCTMSimulator(
            project,
            TheoryCTMScenarioConfig(
                incident_link_id="L_IN",
                incident_capacity_factor=1.0,
                incident_blocked_lanes=1,
            ),
        )

        incident = project.metadata["ctm_incident"]
        self.assertEqual(incident["incident_model"], "lane_blockage")
        self.assertEqual(incident["blocked_lanes"], 1)
        self.assertEqual(incident["lanes_total"], 2)
        self.assertAlmostEqual(incident["capacity_factor"], 0.5)

    def test_lane_blockage_can_close_single_lane_link(self):
        project = movement_test_project()
        project.network.links["L_IN"].parameters = {"lanes_total": 1}

        TheoryCTMSimulator(
            project,
            TheoryCTMScenarioConfig(
                incident_link_id="L_IN",
                incident_capacity_factor=1.0,
                incident_blocked_lanes=1,
            ),
        )

        self.assertAlmostEqual(project.metadata["ctm_incident"]["capacity_factor"], 0.0)

    def test_baseline_direct_capacity_factor_can_remain_one(self):
        project = movement_test_project()
        TheoryCTMSimulator(
            project,
            TheoryCTMScenarioConfig(
                incident_link_id="L_IN",
                incident_capacity_factor=1.0,
                incident_blocked_lanes=None,
            ),
        )

        incident = project.metadata["ctm_incident"]
        self.assertEqual(incident["incident_model"], "direct_capacity_factor")
        self.assertAlmostEqual(incident["capacity_factor"], 1.0)


class CTMExperimentRunnerTest(unittest.TestCase):
    def test_apply_lane_changes_updates_link_and_preserves_base_lanes(self):
        project = source_inflow_test_project()

        apply_lane_changes(project, {"L_SRC_FAST": 1})

        link = project.network.links["L_SRC_FAST"]
        self.assertEqual(link.parameters["lanes_total_base"], 1)
        self.assertEqual(link.parameters["lanes_total"], 2)
        self.assertEqual(link.parameters["lanes_total_scenario"], 2)
        self.assertEqual(link.metadata["lane_scenario_delta"], 1)

    def test_apply_lane_changes_rejects_unknown_link(self):
        project = source_inflow_test_project()

        with self.assertRaises(ValueError):
            apply_lane_changes(project, {"UNKNOWN": 1})


class CTMSourceInflowAllocationTest(unittest.TestCase):
    def test_split_total_by_capacity_is_default(self):
        project = source_inflow_test_project()
        simulator = CTMSimulator(project, CTMScenarioConfig(inflow_pcu_per_hour=2200.0))

        fast_capacity = simulator.ctm_links["L_SRC_FAST"].diagram.capacity * 3600.0
        slow_capacity = simulator.ctm_links["L_SRC_SLOW"].diagram.capacity * 3600.0
        capacity_sum = fast_capacity + slow_capacity
        expected_fast = 2200.0 * fast_capacity / capacity_sum
        expected_slow = 2200.0 * slow_capacity / capacity_sum

        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_FAST"] * 3600.0, expected_fast)
        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_SLOW"] * 3600.0, expected_slow)
        self.assertEqual(project.metadata["ctm_source_inflow_allocation"], "split_total_by_capacity")
        self.assertAlmostEqual(
            sum(project.metadata["ctm_source_inflows_pcu_h"].values()),
            2200.0,
            places=3,
        )

    def test_split_total_equal_divides_total_between_sources(self):
        project = source_inflow_test_project()
        simulator = CTMSimulator(
            project,
            CTMScenarioConfig(
                inflow_pcu_per_hour=1800.0,
                source_inflow_allocation="split_total_equal",
            ),
        )

        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_FAST"] * 3600.0, 900.0)
        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_SLOW"] * 3600.0, 900.0)

    def test_uniform_per_source_preserves_legacy_semantics(self):
        project = source_inflow_test_project()
        simulator = CTMSimulator(
            project,
            CTMScenarioConfig(
                inflow_pcu_per_hour=1800.0,
                source_inflow_allocation="uniform_per_source",
            ),
        )

        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_FAST"] * 3600.0, 1800.0)
        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_SLOW"] * 3600.0, 1800.0)

    def test_manual_source_inflow_override_uses_remaining_total(self):
        project = source_inflow_test_project()
        simulator = CTMSimulator(
            project,
            CTMScenarioConfig(
                inflow_pcu_per_hour=2200.0,
                source_inflows_pcu_per_hour={"L_SRC_FAST": 700.0},
            ),
        )

        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_FAST"] * 3600.0, 700.0)
        self.assertAlmostEqual(simulator.source_inflow_rates_pcu_s["L_SRC_SLOW"] * 3600.0, 1500.0)

    def test_step_generates_total_demand_once_in_split_total_mode(self):
        project = source_inflow_test_project()
        simulator = CTMSimulator(
            project,
            CTMScenarioConfig(
                dt_seconds=1.0,
                inflow_pcu_per_hour=3600.0,
                source_inflow_allocation="split_total_equal",
            ),
        )

        simulator.step(0.0)

        self.assertAlmostEqual(simulator.mass_generated, 1.0)
        self.assertTrue(simulator.movements)
        self.assertTrue(all(movement["history_flow_pcu_h"] for movement in simulator.movements))


class CTMPartialFIFOTest(unittest.TestCase):
    def _solve_once(self, fifo_strength):
        project = movement_test_project()
        simulator = CTMSimulator(project, CTMScenarioConfig(fifo_strength=fifo_strength))
        demands = {link_id: 0.0 for link_id in simulator.ctm_links}
        supplies = {link_id: 10.0 for link_id in simulator.ctm_links}
        demands["L_IN"] = 1.0
        supplies["L_STRAIGHT"] = 0.05
        actual_inflows = {link_id: 0.0 for link_id in simulator.ctm_links}
        actual_outflows = {link_id: 0.0 for link_id in simulator.ctm_links}

        simulator._solve_nodes(demands, supplies, actual_inflows, actual_outflows)
        return simulator, actual_inflows, actual_outflows

    def test_diverge_node_uses_fifo_solver(self):
        simulator, actual_inflows, actual_outflows = self._solve_once(0.0)
        movements = {
            movement["out_link_id"]: movement
            for movement in simulator.movements_by_node["N"]["L_IN"]
        }
        expected_outflow = 0.05 / movements["L_STRAIGHT"]["turn_ratio"]

        self.assertAlmostEqual(actual_inflows["L_STRAIGHT"], 0.05)
        self.assertAlmostEqual(actual_outflows["L_IN"], expected_outflow)
        self.assertEqual(simulator.node_solver_case_counts, {"diverge_fifo": 1})
        self.assertTrue(all(movement["blocked_by_supply_count"] > 0 for movement in movements.values()))

    def test_diverge_node_applies_same_restriction_factor_to_incoming(self):
        simulator, actual_inflows, actual_outflows = self._solve_once(1.0)
        movements = simulator.movements_by_node["N"]["L_IN"]
        fifo_factor = min(movement["min_fifo_factor"] for movement in movements)

        self.assertAlmostEqual(actual_outflows["L_IN"], fifo_factor)
        for movement in movements:
            self.assertAlmostEqual(movement["min_restriction_factor"], fifo_factor)
        self.assertLess(actual_inflows["L_LEFT"], movements[1]["turn_ratio"])

    def test_fifo_strength_does_not_change_explicit_diverge_case(self):
        _, _, nonfifo_outflows = self._solve_once(0.0)
        _, _, half_fifo_outflows = self._solve_once(0.5)
        _, _, strict_fifo_outflows = self._solve_once(1.0)

        self.assertAlmostEqual(nonfifo_outflows["L_IN"], half_fifo_outflows["L_IN"])
        self.assertAlmostEqual(half_fifo_outflows["L_IN"], strict_fifo_outflows["L_IN"])

    def test_diverge_diagnostics_record_factor_ranges(self):
        simulator, _, _ = self._solve_once(0.5)
        movement = next(
            movement
            for movement in simulator.movements_by_node["N"]["L_IN"]
            if movement["out_link_id"] == "L_LEFT"
        )

        self.assertLess(movement["min_fifo_factor"], 1.0)
        self.assertAlmostEqual(movement["min_nonfifo_factor"], movement["min_fifo_factor"])
        self.assertAlmostEqual(movement["min_restriction_factor"], movement["min_fifo_factor"])
        self.assertIn("supply:L_STRAIGHT", movement["active_constraints"])

    def test_factor_diagnostics_ignore_zero_desired_flow_steps(self):
        project = movement_test_project()
        simulator = CTMSimulator(project, CTMScenarioConfig(fifo_strength=1.0))
        demands = {link_id: 0.0 for link_id in simulator.ctm_links}
        supplies = {link_id: 0.0 for link_id in simulator.ctm_links}
        actual_inflows = {link_id: 0.0 for link_id in simulator.ctm_links}
        actual_outflows = {link_id: 0.0 for link_id in simulator.ctm_links}

        simulator._solve_nodes(demands, supplies, actual_inflows, actual_outflows)
        movement = simulator.movements_by_node["N"]["L_IN"][0]

        self.assertEqual(movement["_flow_sample_count"], 1)
        self.assertEqual(movement["_factor_sample_count"], 0)
        self.assertEqual(movement["blocked_by_supply_count"], 0)
        self.assertEqual(movement["fifo_limited_count"], 0)
        self.assertEqual(movement["potential_fifo_limited_count"], 0)
        self.assertEqual(movement["min_fifo_factor"], 1.0)
        self.assertEqual(movement["min_nonfifo_factor"], 1.0)
        self.assertEqual(movement["min_restriction_factor"], 1.0)


class CTMSimulatorRegressionTest(unittest.TestCase):
    def test_source_queue_accumulates_unadmitted_boundary_demand(self):
        project = ProjectLoader().load("osm_network_project_map_nstu.json")
        simulator = CTMSimulator(project, CTMScenarioConfig(simulation_minutes=50))
        blocked_source_id = simulator.sources[0]
        blocked_source = simulator.ctm_links[blocked_source_id]
        blocked_source.cells[0].density = blocked_source.diagram.jam_density

        simulator.step(0.0)

        external_queue = sum(
            simulator.ctm_links[source_id].external_queue
            for source_id in simulator.sources
        )
        self.assertGreater(blocked_source.external_queue, 0.0)
        self.assertAlmostEqual(
            simulator.mass_generated - simulator.mass_entered - external_queue,
            0.0,
            places=9,
        )

    def test_nstu_simulator_uses_strict_core_and_preserves_mass(self):
        project = ProjectLoader().load("osm_network_project_map_nstu.json")
        simulator = CTMSimulator(
            project,
            CTMScenarioConfig(
                simulation_minutes=50,
                source_inflow_allocation="uniform_per_source",
                incident_link_id="L9",
            ),
        )

        simulator.run()

        metadata = project.metadata["ctm_simulation"]
        self.assertLess(abs(metadata["conservation_error_pcu"]), 0.01)
        self.assertLess(abs(metadata["source_queue_balance_error_pcu"]), 0.01)
        self.assertLess(abs(metadata["demand_balance_error_pcu"]), 0.01)
        self.assertLess(abs(metadata["sum_link_conservation_error_pcu"]), 0.01)
        self.assertTrue(metadata["validate_cfl"])
        self.assertEqual(metadata["simulation_minutes"], 50)
        self.assertEqual(
            metadata["node_solver"],
            "explicit_diverge_merge_general_ctm_node_solver",
        )
        self.assertEqual(metadata["fifo_strength"], 0.0)
        self.assertIn("total_generated_pcu", metadata)
        self.assertIn("total_external_queue_pcu", metadata)
        self.assertIn("ctm_scenario_config", project.metadata)
        self.assertIn("ctm_movements", project.metadata)
        self.assertIn("ctm_movement_summary", project.metadata)
        self.assertTrue(project.metadata["ctm_movements"])

        l7_movements = [
            movement
            for movement in project.metadata["ctm_movements"]
            if movement["in_link_id"] == "L7"
        ]
        self.assertTrue(l7_movements)
        self.assertTrue(all("avg_flow_pcu_h" in movement for movement in l7_movements))
        self.assertTrue(all("blocked_by_supply_count" in movement for movement in l7_movements))
        self.assertTrue(all("avg_fifo_factor" in movement for movement in l7_movements))
        self.assertTrue(all("avg_nonfifo_factor" in movement for movement in l7_movements))
        self.assertTrue(all("avg_restriction_factor" in movement for movement in l7_movements))

        summary = project.metadata["ctm_movement_summary"]
        self.assertEqual(
            summary["node_solver"],
            "explicit_diverge_merge_general_ctm_node_solver",
        )
        self.assertIn("turn_ratio_gt_0_9_count", summary)
        self.assertIn("turn_ratio_gt_0_95_count", summary)

        incident = project.metadata["ctm_incident"]
        link = project.network.links[incident["link_id"]]
        history = link.results["history_cells_density_pcu_km"]
        incident_cell = incident["cell_index"]
        upstream_max = max(
            max(snapshot[:incident_cell])
            for snapshot in history[6:15]
            if snapshot[:incident_cell]
        )
        self.assertGreater(upstream_max, 100.0)
        self.assertEqual(len(history), 50)
        for source_id in simulator.sources:
            source_link = project.network.links[source_id]
            self.assertEqual(len(source_link.results["history_external_queue_pcu"]), 50)


if __name__ == "__main__":
    unittest.main()
