from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import math

from ctm_fd import make_triangular_fd_from_capacity
from ctm_network_core_v2 import CTMModel, CTMStateError, Incident
from ctm_node_solver import NodeMovement, solve_ctm_node
from ctm_simulator_test import (
    CTMScenarioConfig as BaseCTMScenarioConfig,
    CTMSimulator as BaseCTMSimulator,
    EPS,
)
from models import Link, Project


THEORY_NODE_SOLVER_NAME = "explicit_diverge_merge_general_ctm_node_solver"
DEFAULT_MOVEMENT_CAPACITY_FACTORS = {
    "same_road_continuation": 1.00,
    "straight": 1.00,
    "right": 0.85,
    "left": 0.65,
    "u_turn": 0.00,
}


@dataclass
class CTMScenarioConfig(BaseCTMScenarioConfig):
    """Scenario config with controlled theory-oriented extensions.

    The model now uses a consistent triangular fundamental diagram. For each
    link, the scenario provides interpretable inputs v, Q and rho_jam; the
    backward wave speed w is derived from the triangular FD relation instead of
    being treated as another independent coefficient.

    Additional scenario assumptions:
    - incident_blocked_lanes: optional lane-aware incident severity;
    - movement_capacity_factors: optional finite saturation limits for movements.
    """

    incident_blocked_lanes: int | None = None
    movement_capacity_factors: dict[str, float] = field(
        default_factory=lambda: deepcopy(DEFAULT_MOVEMENT_CAPACITY_FACTORS)
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.incident_blocked_lanes is not None and self.incident_blocked_lanes < 0:
            raise ValueError("incident_blocked_lanes must be non-negative or None")
        for turn_type, factor in self.movement_capacity_factors.items():
            if factor < 0.0:
                raise ValueError(f"movement_capacity_factors[{turn_type!r}] must be non-negative")


class CTMSimulator(BaseCTMSimulator):
    """Theory-oriented network CTM simulator.

    Compared with the historical ctm_simulator_test.py implementation, this
    class keeps the conservative link update but changes the theoretical model:
    - each link FD is a self-consistent triangular diagram;
    - diverge nodes use a FIFO split formula;
    - merge nodes use priority allocation under downstream supply;
    - arbitrary many-to-many nodes use a proportional fallback.
    """

    config: CTMScenarioConfig

    def __init__(self, project: Project, config: CTMScenarioConfig | None = None):
        super().__init__(project, config or CTMScenarioConfig())
        self.project.metadata["node_solver"] = THEORY_NODE_SOLVER_NAME

    def _init_physics(self) -> None:
        print("Initializing theory-oriented CTM link models...")
        fd_metadata = {}
        for link in self.network.links.values():
            hw = link.metadata.get("highway", "default")
            params = self.config.highway_params.get(hw, self.config.highway_params["default"])
            lanes = max(int(link.parameters.get("lanes_total", 1) or 1), 1)

            capacity_pcu_h = float(params["cap_per_lane"]) * lanes
            jam_density_pcu_km = float(self.config.jam_density_pcu_km_per_lane) * lanes
            diagram, diagram_metadata = make_triangular_fd_from_capacity(
                free_flow_speed_kph=float(params["speed_kph"]),
                capacity_pcu_h=capacity_pcu_h,
                jam_density_pcu_km=jam_density_pcu_km,
            )

            max_wave_speed = max(diagram.free_flow_speed, diagram.backward_wave_speed)
            min_cfl_cell_length = self.dt * max_wave_speed
            cell_length_target = max(self.config.cell_length_target_m, min_cfl_cell_length)
            length_m = max(link.length_km * 1000.0, min_cfl_cell_length)
            cell_count = max(1, math.floor(length_m / cell_length_target))

            ctm = CTMModel.create_uniform_link(
                length=length_m,
                cell_length=length_m / cell_count,
                diagram=diagram,
                dt=self.dt,
                validate_cfl=True,
            )

            link.results = {
                "cell_count": cell_count,
                "history_cells_density_pcu_km": [],
                "history_flow_veh_h": [],
                "ctm_length_m": round(length_m, 3),
                "ctm_cell_length_m": round(length_m / cell_count, 3),
                "fundamental_diagram": dict(diagram_metadata.__dict__),
            }
            fd_metadata[link.id] = dict(diagram_metadata.__dict__)
            self.ctm_links[link.id] = ctm
        self.project.metadata["ctm_fundamental_diagram_model"] = {
            "parameterization": "v_Q_rhojam_with_derived_w",
            "formula": "Q = v*w/(v+w)*rho_jam; w = Q/(rho_jam - Q/v)",
            "links": fd_metadata,
        }

    def _add_movement(
        self,
        node_id: str,
        in_link: Link,
        out_link: Link,
        turn_type: str,
        angle_deg: float,
        raw_score: float,
        turn_ratio: float,
        source: str,
        reason: list[str],
        flags: list[str],
    ) -> None:
        super()._add_movement(
            node_id=node_id,
            in_link=in_link,
            out_link=out_link,
            turn_type=turn_type,
            angle_deg=angle_deg,
            raw_score=raw_score,
            turn_ratio=turn_ratio,
            source=source,
            reason=reason,
            flags=flags,
        )
        movement = self.movements[-1]
        movement_capacity_pcu_s = self._movement_capacity_pcu_s(in_link, out_link, turn_type)
        movement["movement_capacity_pcu_h"] = round(movement_capacity_pcu_s * 3600.0, 3)
        movement["movement_capacity_limited_count"] = 0
        movement["node_solver_case"] = ""
        movement["active_constraints"] = []

    def _movement_capacity_pcu_s(self, in_link: Link, out_link: Link, turn_type: str) -> float:
        factor = self.config.movement_capacity_factors.get(turn_type, 1.0)
        if factor <= 0.0:
            return 0.0
        in_ctm = self.ctm_links.get(in_link.id)
        out_ctm = self.ctm_links.get(out_link.id)
        if in_ctm is None or out_ctm is None:
            return float("inf")
        return min(in_ctm.diagram.capacity, out_ctm.diagram.capacity) * factor

    def _effective_incident_capacity_factor(self, link: Link) -> float:
        blocked_lanes = self.config.incident_blocked_lanes
        if blocked_lanes is not None:
            lanes = max(int(link.parameters.get("lanes_total", 1) or 1), 1)
            blocked = min(max(int(blocked_lanes), 0), lanes)
            return (lanes - blocked) / lanes

        return float(self.config.incident_capacity_factor)

    def _plan_incident(self) -> None:
        if self.config.incident_link_id is not None:
            if self.config.incident_link_id not in self.network.links:
                raise CTMStateError(f"configured incident link {self.config.incident_link_id} does not exist")
            incident_link = self.network.links[self.config.incident_link_id]
        else:
            candidates = [
                link for link in self.network.links.values()
                if link.id not in self.sources and link.id not in self.sinks
            ]
            if not candidates:
                return
            incident_link = max(candidates, key=lambda link: link.length_km)

        self.incident_link_id = incident_link.id
        ctm = self.ctm_links[self.incident_link_id]
        self.incident_cell_index = ctm.cell_count // 2
        capacity_factor = self._effective_incident_capacity_factor(incident_link)
        incident = Incident(
            cell_index=self.incident_cell_index,
            start_time=self.config.incident_start_sec,
            end_time=self.config.incident_end_sec,
            capacity_factor=capacity_factor,
            speed_factor=self.config.incident_speed_factor,
        )
        ctm.incidents.append(incident)
        incident_data = {
            "incident_model": (
                "lane_blockage"
                if self.config.incident_blocked_lanes is not None
                else "direct_capacity_factor"
            ),
            "cell_index": self.incident_cell_index,
            "start_time_sec": incident.start_time,
            "end_time_sec": incident.end_time,
            "capacity_factor": incident.capacity_factor,
            "configured_capacity_factor": self.config.incident_capacity_factor,
            "blocked_lanes": self.config.incident_blocked_lanes,
            "lanes_total": int(incident_link.parameters.get("lanes_total", 1) or 1),
            "speed_factor": incident.speed_factor,
        }
        incident_link.results["incident"] = dict(incident_data)
        self.project.metadata["ctm_incident"] = {
            "link_id": incident_link.id,
            "link_name": incident_link.name,
            **incident_data,
        }

        print(
            f"Incident planned on {incident_link.id} ({incident_link.name}), "
            f"cell {self.incident_cell_index}, {incident.start_time:.0f}-{incident.end_time:.0f}s, "
            f"capacity factor {capacity_factor:.3f}."
        )

    def _solve_nodes(
        self,
        demands: dict[str, float],
        supplies: dict[str, float],
        actual_inflows: dict[str, float],
        actual_outflows: dict[str, float],
    ) -> None:
        case_counts: dict[str, int] = getattr(self, "node_solver_case_counts", {})
        for movements_by_in_link in self.movements_by_node.values():
            flat_movements = [movement for movements in movements_by_in_link.values() for movement in movements]
            if not flat_movements:
                continue

            solver_movements: list[NodeMovement] = []
            movement_by_key = {}
            for movement in flat_movements:
                key = (movement["in_link_id"], movement["out_link_id"])
                movement_by_key[key] = movement
                solver_movements.append(
                    NodeMovement(
                        in_link_id=movement["in_link_id"],
                        out_link_id=movement["out_link_id"],
                        turn_ratio=float(movement["turn_ratio"]),
                        priority=self._movement_priority(movement),
                        metadata={"turn_type": movement.get("turn_type", "")},
                    )
                )

            result = solve_ctm_node(
                solver_movements,
                demands,
                supplies,
                fifo_strength=self.config.fifo_strength,
            )
            case_counts[result.case] = case_counts.get(result.case, 0) + 1

            for key, flow in result.flows.items():
                in_id, out_id = key
                movement = movement_by_key[key]
                movement_capacity = float(movement.get("movement_capacity_pcu_h", float("inf"))) / 3600.0
                limited_flow = min(flow, movement_capacity)
                diag = result.diagnostics[key]
                active_constraints = list(diag.active_constraints)
                capacity_limited = flow > limited_flow + EPS
                if capacity_limited:
                    active_constraints.append("movement_capacity")
                    movement["movement_capacity_limited_count"] = movement.get("movement_capacity_limited_count", 0) + 1

                actual_inflows[out_id] += limited_flow
                actual_outflows[in_id] += limited_flow

                movement["node_solver_case"] = result.case
                movement["active_constraints"] = sorted(set(active_constraints))
                movement["blocked_by_supply_count"] += int(
                    any(item.startswith("supply:") for item in active_constraints)
                )
                if any(item.startswith("fifo:") for item in active_constraints):
                    movement["fifo_limited_count"] += 1

                restriction_factor = 1.0 if diag.desired_flow <= EPS else limited_flow / diag.desired_flow
                self._record_movement_step(
                    movement=movement,
                    actual_flow=limited_flow,
                    desired_flow=diag.desired_flow,
                    fifo_factor=restriction_factor,
                    nonfifo_factor=restriction_factor,
                    restriction_factor=restriction_factor,
                )

        self.node_solver_case_counts = case_counts

    def _movement_priority(self, movement: dict) -> float:
        """Priority used only for merge nodes.

        To avoid adding another arbitrary dataset, priority is simply derived
        from the existing turn ratio. Equal turn ratios imply equal merge
        priority; explicit turn-ratio overrides therefore also define the merge
        priority in controlled scenarios.
        """

        return max(float(movement.get("turn_ratio", 0.0)), EPS)

    def _movement_summary(self) -> dict[str, object]:
        summary = super()._movement_summary()
        summary["node_solver"] = THEORY_NODE_SOLVER_NAME
        summary["node_solver_case_counts"] = getattr(self, "node_solver_case_counts", {})
        summary["movement_capacity_model"] = "turn_type_factor_times_min_link_capacity"
        summary["movement_capacity_limited_count"] = sum(
            movement.get("movement_capacity_limited_count", 0) for movement in self.movements
        )
        summary["incident_model"] = "lane_blockage_or_direct_capacity_factor"
        return summary

    def run(self) -> None:
        super().run()
        self.project.metadata["node_solver"] = THEORY_NODE_SOLVER_NAME
        self.project.metadata.setdefault("ctm_movement_summary", {})["node_solver"] = THEORY_NODE_SOLVER_NAME
        self.project.metadata.setdefault("ctm_movement_summary", {})[
            "node_solver_case_counts"
        ] = getattr(self, "node_solver_case_counts", {})
        self.project.metadata.setdefault("ctm_simulation", {})["node_solver"] = THEORY_NODE_SOLVER_NAME
        self.project.metadata.setdefault("ctm_simulation", {})[
            "node_solver_case_counts"
        ] = getattr(self, "node_solver_case_counts", {})
