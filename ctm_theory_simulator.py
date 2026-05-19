from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from ctm_network_core_v2 import CTMStateError, Incident
from ctm_simulator_test import (
    CTMScenarioConfig as BaseCTMScenarioConfig,
    CTMSimulator as BaseCTMSimulator,
    EPS,
    NODE_SOLVER_NAME,
)
from models import Link, Project


DEFAULT_MOVEMENT_CAPACITY_FACTORS = {
    "same_road_continuation": 1.00,
    "straight": 1.00,
    "right": 0.85,
    "left": 0.65,
    "u_turn": 0.00,
}


@dataclass
class CTMScenarioConfig(BaseCTMScenarioConfig):
    """Scenario config with two theory-oriented additions.

    1. incident_blocked_lanes makes an incident lane-aware. If it is set, an
       incident does not automatically reduce the whole link to one scalar
       capacity_factor. Instead, capacity is reduced according to the share of
       lanes still open. If all lanes are blocked, incident_capacity_factor is
       used as the residual emergency capacity.

    2. movement_capacity_factors add finite capacities to intersection
       movements. This keeps the node from being only a zero-storage splitter:
       left/right/through movements can have different saturation assumptions.
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
    """CTM simulator with lane-aware incidents and movement capacities.

    It intentionally reuses the existing link CTM core and most network logic.
    The changes are limited to places where the previous model was too far from
    traffic-flow theory:
    - incidents can close part of a road instead of implicitly affecting all lanes;
    - node movements have finite saturation capacities.
    """

    config: CTMScenarioConfig

    def __init__(self, project: Project, config: CTMScenarioConfig | None = None):
        super().__init__(project, config or CTMScenarioConfig())

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

    def _movement_capacity_pcu_s(self, in_link: Link, out_link: Link, turn_type: str) -> float:
        """Finite saturation capacity for one node movement.

        This is not a full signal/lane-group model. It is a bounded movement
        capacity derived from the smaller of incoming/outgoing link capacities
        and a turn-type factor. It makes intersections finite without requiring
        OD matrices or detailed lane data.
        """

        factor = self.config.movement_capacity_factors.get(turn_type, 1.0)
        if factor <= 0.0:
            return 0.0
        in_ctm = self.ctm_links.get(in_link.id)
        out_ctm = self.ctm_links.get(out_link.id)
        if in_ctm is None or out_ctm is None:
            return float("inf")
        return min(in_ctm.diagram.capacity, out_ctm.diagram.capacity) * factor

    def _effective_incident_capacity_factor(self, link: Link) -> float:
        """Return capacity factor with optional lane-aware closure logic."""

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
        for movements_by_in_link in self.movements_by_node.values():
            outlink_total_demand = defaultdict(float)
            desired_by_movement: dict[int, float] = {}
            nonfifo_factor_by_movement: dict[int, float] = {}
            fifo_factor_by_in_link: dict[str, float] = {}

            for in_id, movements in movements_by_in_link.items():
                for movement in movements:
                    out_id = movement["out_link_id"]
                    base_desired_flow = demands[in_id] * movement["turn_ratio"]
                    movement_capacity = float(movement.get("movement_capacity_pcu_h", float("inf"))) / 3600.0
                    desired_flow = min(base_desired_flow, movement_capacity)
                    if base_desired_flow > desired_flow + EPS:
                        movement["movement_capacity_limited_count"] = movement.get("movement_capacity_limited_count", 0) + 1
                    desired_by_movement[id(movement)] = desired_flow
                    outlink_total_demand[out_id] += desired_flow

            for in_id, movements in movements_by_in_link.items():
                incoming_factors = []
                for movement in movements:
                    out_id = movement["out_link_id"]
                    total_demand = outlink_total_demand[out_id]
                    factor = 1.0 if total_demand <= 0.0 else min(1.0, supplies[out_id] / total_demand)
                    nonfifo_factor_by_movement[id(movement)] = factor
                    incoming_factors.append(factor)
                fifo_factor_by_in_link[in_id] = min(incoming_factors) if incoming_factors else 1.0

            for in_id, movements in movements_by_in_link.items():
                fifo_factor = fifo_factor_by_in_link[in_id]
                for movement in movements:
                    out_id = movement["out_link_id"]
                    desired_flow = desired_by_movement[id(movement)]
                    nonfifo_factor = nonfifo_factor_by_movement[id(movement)]
                    restriction_factor = (
                        (1.0 - self.config.fifo_strength) * nonfifo_factor
                        + self.config.fifo_strength * fifo_factor
                    )
                    actual_flow = desired_flow * restriction_factor

                    has_desired_flow = desired_flow > EPS
                    if has_desired_flow and nonfifo_factor < 1.0 - EPS:
                        movement["blocked_by_supply_count"] += 1
                    if has_desired_flow and fifo_factor < nonfifo_factor - EPS:
                        movement["potential_fifo_limited_count"] += 1
                    if (
                        has_desired_flow
                        and self.config.fifo_strength > 0.0
                        and fifo_factor < nonfifo_factor - EPS
                    ):
                        movement["fifo_limited_count"] += 1

                    actual_inflows[out_id] += actual_flow
                    actual_outflows[in_id] += actual_flow
                    self._record_movement_step(
                        movement=movement,
                        actual_flow=actual_flow,
                        desired_flow=desired_flow,
                        fifo_factor=fifo_factor,
                        nonfifo_factor=nonfifo_factor,
                        restriction_factor=restriction_factor,
                    )

    def _movement_summary(self) -> dict[str, Any]:
        summary = super()._movement_summary()
        summary["movement_capacity_model"] = "turn_type_factor_times_min_link_capacity"
        summary["movement_capacity_limited_count"] = sum(
            movement.get("movement_capacity_limited_count", 0) for movement in self.movements
        )
        summary["incident_model"] = "lane_aware_capacity_drop"
        return summary
