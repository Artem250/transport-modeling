from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

from ctm_network_core_v2 import CTMStateError, Incident
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
    """Scenario config with two controlled extensions.

    The default model still uses the same inputs as the earlier simulator. The
    additional parameters are intentionally small and explicit:
    - incident_blocked_lanes: optional lane-aware incident severity;
    - movement_capacity_factors: optional finite saturation limits for movements.

    They are not claimed to be calibrated field data. They are scenario
    assumptions for controlled computational experiments.
    """

    incident_blocked_lanes: int | None = 1
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
    class keeps the link CTM unchanged but replaces the implicit node heuristic
    with an explicit solver:
    - diverge: FIFO split formula;
    - merge: priority allocation under downstream supply;
    - general node: proportional fallback with optional partial FIFO.
    """

    config: CTMScenarioConfig

    def __init__(self, project: Project, config: CTMScenarioConfig | None = None):
        super().__init__(project, config or CTMScenarioConfig())
        self.project.metadata["node_solver"] = THEORY_NODE_SOLVER_NAME

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
        direct_factor = float(self.config.incident_capacity_factor)
        if direct_factor >= 1.0:
            return 1.0

        blocked_lanes = self.config.incident_blocked_lanes
        if blocked_lanes is None:
            return direct_factor

        lanes = max(int(link.parameters.get("lanes_total", 1) or 1), 1)
        blocked = min(max(int(blocked_lanes), 0), lanes)
        open_lanes = lanes - blocked
        if open_lanes > 0:
            return open_lanes / lanes
        return direct_factor

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
                if flow > limited_flow + EPS:
                    movement["movement_capacity_limited_count"] = movement.get("movement_capacity_limited_count", 0) + 1
                    active_constraints = list(result.diagnostics[key].active_constraints) + ["movement_capacity"]
                else:
                    active_constraints = list(result.diagnostics[key].active_constraints)

                actual_inflows[out_id] += limited_flow
                actual_outflows[in_id] += limited_flow

                movement["node_solver_case"] = result.case
                movement["active_constraints"] = sorted(set(active_constraints))
                diag = result.diagnostics[key]
                movement["blocked_by_supply_count"] += sum(
                    1 for item in active_constraints if item.startswith("supply:")
                )
                if any(item.startswith("fifo:") for item in active_constraints):
                    movement["fifo_limited_count"] += 1
                if diag.desired_flow > limited_flow + EPS and "movement_capacity" in active_constraints:
                    movement["movement_capacity_limited_count"] = movement.get("movement_capacity_limited_count", 0) + 1

                self._record_movement_step(
                    movement=movement,
                    actual_flow=limited_flow,
                    desired_flow=diag.desired_flow,
                    fifo_factor=diag.restriction_factor,
                    nonfifo_factor=diag.restriction_factor,
                    restriction_factor=diag.restriction_factor,
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
        summary["incident_model"] = "lane_aware_capacity_drop"
        return summary
