import unittest

from ctm_network_core_v2 import (
    CTMModel,
    CTMStateError,
    Incident,
    TriangularFundamentalDiagram,
)
from ctm_simulator_test import CTMSimulator
from project_loader import ProjectLoader


def diagram():
    return TriangularFundamentalDiagram.from_common_units(
        free_flow_speed_kph=60.0,
        backward_wave_speed_kph=18.0,
        capacity_pcu_h=1800.0,
        jam_density_pcu_km=150.0,
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


class CTMSimulatorRegressionTest(unittest.TestCase):
    def test_source_queue_accumulates_unadmitted_boundary_demand(self):
        project = ProjectLoader().load("osm_network_project_map_nstu.json")
        simulator = CTMSimulator(project)
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
        simulator = CTMSimulator(project)

        simulator.run()

        metadata = project.metadata["ctm_simulation"]
        self.assertLess(abs(metadata["conservation_error_pcu"]), 0.01)
        self.assertLess(abs(metadata["source_queue_balance_error_pcu"]), 0.01)
        self.assertLess(abs(metadata["demand_balance_error_pcu"]), 0.01)
        self.assertLess(abs(metadata["sum_link_conservation_error_pcu"]), 0.01)
        self.assertTrue(metadata["validate_cfl"])
        self.assertIn("total_generated_pcu", metadata)
        self.assertIn("total_external_queue_pcu", metadata)

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
