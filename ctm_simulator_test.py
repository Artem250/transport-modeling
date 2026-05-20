from __future__ import annotations

import math
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any

from models import Link, Project
from project_loader import ProjectLoader
from project_saver import ProjectSaver

from ctm_network_core_v2 import (
    CTMModel,
    CTMStateError,
    Incident,
    TriangularFundamentalDiagram,
)


DT_SECONDS = 0.5
SIMULATION_MINUTES = 100
SNAPSHOT_INTERVAL_SEC = 60
CELL_LENGTH_TARGET = 15.0
INFLOW_VEH_PER_HOUR = 475.0

# HIGHWAY_PARAMS = {
#     "primary": {"speed_kph": 60, "cap_per_lane": 1000, "weight": 3.0},
#     "secondary": {"speed_kph": 50, "cap_per_lane": 800, "weight": 2.0},
#     "tertiary": {"speed_kph": 40, "cap_per_lane": 700, "weight": 1.0},
#     "residential": {"speed_kph": 30, "cap_per_lane": 500, "weight": 0.5},
#     "trunk": {"speed_kph": 80, "cap_per_lane": 1400, "weight": 4.0},
#     "default": {"speed_kph": 40, "cap_per_lane": 600, "weight": 1.0},
# }


HIGHWAY_PARAMS = {
    "trunk":       {"speed_kph": 80, "cap_per_lane": 1800, "weight": 4.0},
    "primary":     {"speed_kph": 60, "cap_per_lane": 1500, "weight": 3.0},
    "secondary":   {"speed_kph": 50, "cap_per_lane": 1200, "weight": 2.0},
    "tertiary":    {"speed_kph": 40, "cap_per_lane": 1000, "weight": 1.0},
    "residential": {"speed_kph": 40, "cap_per_lane": 700,  "weight": 0.5},
    "default":     {"speed_kph": 40, "cap_per_lane": 1000, "weight": 1.0},
}

TURN_WEIGHTS = {
    "same_road_continuation": 3.0,
    "straight": 2.0,
    "right": 0.7,
    "left": 0.45,
    "u_turn": 0.0,
}

SAME_ROAD_BONUSES = {
    "same_osm_way_id": 3.0,
    "same_osm_name": 2.0,
    "same_visible_name": 1.5,
    "same_highway_straight": 1.25,
}

BACKWARD_WAVE_SPEED_KPH = 18.0
JAM_DENSITY_PCU_KM_PER_LANE = 140.0
SHORT_CONNECTOR_LENGTH_M = 30.0
TURN_RATIO_TOLERANCE = 1e-6
EPS = 1e-12
EARTH_RADIUS_M = 6371008.8
NODE_SOLVER_NAME = "proportional_split_with_optional_partial_fifo"


@dataclass
class CTMScenarioConfig:
    dt_seconds: float = DT_SECONDS
    simulation_minutes: int = SIMULATION_MINUTES
    snapshot_interval_sec: int = SNAPSHOT_INTERVAL_SEC
    cell_length_target_m: float = CELL_LENGTH_TARGET
    inflow_veh_per_hour: float = INFLOW_VEH_PER_HOUR
    highway_params: dict[str, dict[str, float]] = field(default_factory=lambda: deepcopy(HIGHWAY_PARAMS))
    turn_weights: dict[str, float] = field(default_factory=lambda: deepcopy(TURN_WEIGHTS))
    same_road_bonuses: dict[str, float] = field(default_factory=lambda: deepcopy(SAME_ROAD_BONUSES))
    jam_density_pcu_km_per_lane: float = JAM_DENSITY_PCU_KM_PER_LANE
    backward_wave_speed_kph: float = BACKWARD_WAVE_SPEED_KPH
    incident_link_id: str | None = None
    incident_start_sec: float = 300.0
    incident_end_sec: float = 900.0
    incident_capacity_factor: float = 0.1
    incident_speed_factor: float = 1.0
    short_connector_length_m: float = SHORT_CONNECTOR_LENGTH_M
    turn_ratio_tolerance: float = TURN_RATIO_TOLERANCE
    fifo_strength: float = 0.0

    def __post_init__(self) -> None:
        if self.dt_seconds <= 0.0:
            raise ValueError("dt_seconds must be positive")
        if self.simulation_minutes <= 0:
            raise ValueError("simulation_minutes must be positive")
        if self.snapshot_interval_sec <= 0:
            raise ValueError("snapshot_interval_sec must be positive")
        if self.cell_length_target_m <= 0.0:
            raise ValueError("cell_length_target_m must be positive")
        if self.jam_density_pcu_km_per_lane <= 0.0:
            raise ValueError("jam_density_pcu_km_per_lane must be positive")
        if self.backward_wave_speed_kph <= 0.0:
            raise ValueError("backward_wave_speed_kph must be positive")
        if self.incident_end_sec <= self.incident_start_sec:
            raise ValueError("incident_end_sec must be greater than incident_start_sec")
        if not 0.0 <= self.incident_capacity_factor <= 1.0:
            raise ValueError("incident_capacity_factor must be in [0, 1]")
        if self.incident_speed_factor < 0.0:
            raise ValueError("incident_speed_factor must be non-negative")
        if not 0.0 <= self.fifo_strength <= 1.0:
            raise ValueError("fifo_strength must be in [0, 1]")
        if "default" not in self.highway_params:
            raise ValueError("highway_params must include a default entry")
        required_highway_keys = {"speed_kph", "cap_per_lane", "weight"}
        for highway, params in self.highway_params.items():
            missing = required_highway_keys - set(params)
            if missing:
                missing_text = ", ".join(sorted(missing))
                raise ValueError(f"highway_params[{highway!r}] is missing: {missing_text}")
            if params["speed_kph"] <= 0.0:
                raise ValueError(f"highway_params[{highway!r}].speed_kph must be positive")
            if params["cap_per_lane"] <= 0.0:
                raise ValueError(f"highway_params[{highway!r}].cap_per_lane must be positive")
            if params["weight"] <= 0.0:
                raise ValueError(f"highway_params[{highway!r}].weight must be positive")

    def to_metadata(self) -> dict[str, Any]:
        return deepcopy(asdict(self))


class CTMSimulator:
    def __init__(self, project: Project, config: CTMScenarioConfig | None = None):
        self.project = project
        self.network = project.network
        self.config = config or CTMScenarioConfig()
        self.dt = self.config.dt_seconds

        self.ctm_links: dict[str, CTMModel] = {}
        self.turn_ratios = defaultdict(lambda: defaultdict(dict))
        self.movements: list[dict] = []
        self.movements_by_node = defaultdict(lambda: defaultdict(list))
        self.movement_warnings: list[str] = []
        self.sources: list[str] = []
        self.sinks: list[str] = []

        self.mass_generated = 0.0
        self.mass_entered = 0.0
        self.mass_exited = 0.0
        self.network_conservation_error_pcu = 0.0
        self.max_abs_link_conservation_error_pcu = 0.0
        self.incident_link_id = None
        self.incident_cell_index = None

        self.project.metadata["ctm_scenario_config"] = self.config.to_metadata()
        self.project.metadata["node_solver"] = NODE_SOLVER_NAME

        self._init_physics()
        self._build_movements()
        self._init_source_queue_history()
        self._plan_incident()

    def _init_physics(self) -> None:
        print("Initializing CTM link models...")
        for link in self.network.links.values():
            hw = link.metadata.get("highway", "default")
            params = self.config.highway_params.get(hw, self.config.highway_params["default"])
            lanes = link.parameters.get("lanes_total", 1)

            diagram = TriangularFundamentalDiagram.from_common_units(
                free_flow_speed_kph=params["speed_kph"],
                backward_wave_speed_kph=self.config.backward_wave_speed_kph,
                capacity_pcu_h=params["cap_per_lane"] * lanes,
                jam_density_pcu_km=self.config.jam_density_pcu_km_per_lane * lanes,
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
            }
            self.ctm_links[link.id] = ctm

    def _build_movements(self) -> None:
        print("Analyzing network movements...")
        overrides = self.project.metadata.setdefault("turn_ratio_overrides", {}) or {}
        for node_id, node in self.network.nodes.items():
            incoming = self.network.get_incoming_links(node_id)
            outgoing = self.network.get_outgoing_links(node_id)

            neighbors = {link.start_node_id for link in incoming} | {link.end_node_id for link in outgoing}
            node_type = getattr(node, "node_type", "") or ""

            if node_type == "boundary" or len(neighbors) <= 1:
                self.sources.extend([link.id for link in outgoing])
                self.sinks.extend([link.id for link in incoming])
                continue

            for in_link in incoming:
                if self._apply_manual_override(node_id, in_link, outgoing, overrides):
                    continue
                if node_type and node_type != "intersection":
                    self._add_forced_through_movement(node_id, in_link, outgoing, node_type)
                else:
                    self._add_inferred_movements(node_id, in_link, outgoing)

        self._validate_movements()
        self._refresh_movement_metadata()
        print(f"Traffic sources: {len(self.sources)}, sinks: {len(self.sinks)}")

    def _apply_manual_override(
        self,
        node_id: str,
        in_link: Link,
        outgoing: list[Link],
        overrides: dict,
    ) -> bool:
        link_override = (overrides.get(node_id, {}) or {}).get(in_link.id)
        if link_override is None:
            return False

        outgoing_by_id = {link.id: link for link in outgoing}
        ratio_sum = 0.0
        for out_id, ratio in link_override.items():
            if out_id not in outgoing_by_id:
                raise CTMStateError(
                    f"invalid turn_ratio_overrides: {node_id}/{in_link.id} references non-outgoing link {out_id}"
                )
            ratio = float(ratio)
            if not 0.0 <= ratio <= 1.0:
                raise CTMStateError(
                    f"invalid turn_ratio_overrides: ratio for {node_id}/{in_link.id}->{out_id} "
                    f"must be in [0, 1], got {ratio:.9f}"
                )
            ratio_sum += ratio
            out_link = outgoing_by_id[out_id]
            angle_deg = self._calc_turn_angle(in_link, out_link)
            turn_type = self._classify_turn(angle_deg, in_link, out_link)
            self._add_movement(
                node_id=node_id,
                in_link=in_link,
                out_link=out_link,
                turn_type=turn_type,
                angle_deg=angle_deg,
                raw_score=ratio,
                turn_ratio=ratio,
                source="manual",
                reason=["manual_override"],
                flags=["manual_override", *self._movement_flags(in_link, out_link, turn_type)],
            )

        if abs(ratio_sum - 1.0) > self.config.turn_ratio_tolerance:
            raise CTMStateError(
                f"invalid turn_ratio_overrides: ratios for {node_id}/{in_link.id} sum to {ratio_sum:.9f}"
            )
        return True

    def _add_forced_through_movement(
        self,
        node_id: str,
        in_link: Link,
        outgoing: list[Link],
        node_type: str,
    ) -> None:
        candidates = [
            out_link
            for out_link in outgoing
            if out_link.end_node_id != in_link.start_node_id
        ]
        same_road_candidates = [
            out_link
            for out_link in candidates
            if self._same_transport_continuation(in_link, out_link)
        ]
        selected = same_road_candidates if len(same_road_candidates) == 1 else candidates
        if len(selected) != 1:
            self.movement_warnings.append(
                f"ambiguous forced-through movement at {node_id}/{in_link.id} ({node_type})"
            )
            return

        out_link = selected[0]
        angle_deg = self._calc_turn_angle(in_link, out_link)
        turn_type = self._classify_turn(angle_deg, in_link, out_link)
        self._add_movement(
            node_id=node_id,
            in_link=in_link,
            out_link=out_link,
            turn_type=turn_type,
            angle_deg=angle_deg,
            raw_score=1.0,
            turn_ratio=1.0,
            source="inferred",
            reason=["forced_through_node_type"],
            flags=self._movement_flags(in_link, out_link, turn_type),
        )

    def _add_inferred_movements(self, node_id: str, in_link: Link, outgoing: list[Link]) -> None:
        scored_movements = []
        total_score = 0.0
        for out_link in outgoing:
            if out_link.end_node_id == in_link.start_node_id:
                continue

            angle_deg = self._calc_turn_angle(in_link, out_link)
            turn_type = self._classify_turn(angle_deg, in_link, out_link)
            score, reason, flags = self._movement_score(in_link, out_link, turn_type)
            if score <= 0.0:
                continue

            total_score += score
            scored_movements.append((out_link, turn_type, angle_deg, score, reason, flags))

        if total_score <= 0.0:
            self.movement_warnings.append(f"no inferred movement for {node_id}/{in_link.id}")
            return

        for out_link, turn_type, angle_deg, score, reason, flags in scored_movements:
            self._add_movement(
                node_id=node_id,
                in_link=in_link,
                out_link=out_link,
                turn_type=turn_type,
                angle_deg=angle_deg,
                raw_score=score,
                turn_ratio=score / total_score,
                source="inferred",
                reason=reason,
                flags=flags,
            )

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
        movement = {
            "node_id": node_id,
            "in_link_id": in_link.id,
            "out_link_id": out_link.id,
            "turn_type": turn_type,
            "angle_deg": round(angle_deg, 3),
            "raw_score": round(raw_score, 6),
            "turn_ratio": float(turn_ratio),
            "source": source,
            "reason": sorted(set(reason)),
            "flags": sorted(set(flags)),
            "avg_flow_veh_h": 0.0,
            "max_flow_veh_h": 0.0,
            "blocked_by_supply_count": 0,
            "fifo_limited_count": 0,
            "potential_fifo_limited_count": 0,
            "avg_fifo_factor": 1.0,
            "min_fifo_factor": 1.0,
            "avg_nonfifo_factor": 1.0,
            "min_nonfifo_factor": 1.0,
            "avg_restriction_factor": 1.0,
            "min_restriction_factor": 1.0,
            "_flow_sum_veh_h": 0.0,
            "_flow_sample_count": 0,
            "_factor_sample_count": 0,
            "_fifo_factor_sum": 0.0,
            "_nonfifo_factor_sum": 0.0,
            "_restriction_factor_sum": 0.0,
        }
        self.movements.append(movement)
        self.movements_by_node[node_id][in_link.id].append(movement)
        self.turn_ratios[node_id][in_link.id][out_link.id] = float(turn_ratio)

    def _movement_score(self, in_link: Link, out_link: Link, turn_type: str) -> tuple[float, list[str], list[str]]:
        reason = [turn_type]
        flags = self._movement_flags(in_link, out_link, turn_type)
        score = self.config.turn_weights[turn_type]

        if "same_osm_way_id" in flags:
            score *= self.config.same_road_bonuses["same_osm_way_id"]
            reason.append("same_osm_way_id")
        elif "same_osm_name" in flags:
            score *= self.config.same_road_bonuses["same_osm_name"]
            reason.append("same_osm_name")
        elif "same_visible_name" in flags:
            score *= self.config.same_road_bonuses["same_visible_name"]
            reason.append("same_visible_name")
        elif "same_highway_straight" in flags:
            score *= self.config.same_road_bonuses["same_highway_straight"]
            reason.append("same_highway_straight")

        hw = out_link.metadata.get("highway", "default")
        score *= self.config.highway_params.get(hw, self.config.highway_params["default"])["weight"]
        score *= out_link.parameters.get("lanes_total", 1)
        reason.extend(["highway_weight", "lane_weight"])
        return score, reason, flags

    def _movement_flags(self, in_link: Link, out_link: Link, turn_type: str) -> list[str]:
        flags = []
        if self._is_short_connector_candidate(in_link) or self._is_short_connector_candidate(out_link):
            flags.append("short_connector_candidate")

        in_way_id = in_link.metadata.get("osm_way_id")
        if in_way_id and in_way_id == out_link.metadata.get("osm_way_id"):
            flags.append("same_osm_way_id")
        if in_link.metadata.get("osm_name") and in_link.metadata.get("osm_name") == out_link.metadata.get("osm_name"):
            flags.append("same_osm_name")
        if in_link.name and in_link.name == out_link.name:
            flags.append("same_visible_name")
        if (
            turn_type == "straight"
            and in_link.metadata.get("highway")
            and in_link.metadata.get("highway") == out_link.metadata.get("highway")
        ):
            flags.append("same_highway_straight")
        return flags

    def _is_short_connector_candidate(self, link: Link) -> bool:
        if link.length_km * 1000.0 >= self.config.short_connector_length_m:
            return False
        start_node = self.network.nodes.get(link.start_node_id)
        end_node = self.network.nodes.get(link.end_node_id)
        if start_node is None or end_node is None:
            return False
        return start_node.node_type == "intersection" and end_node.node_type == "intersection"

    def _classify_turn(self, angle_deg: float, in_link: Link, out_link: Link) -> str:
        if out_link.end_node_id == in_link.start_node_id:
            return "u_turn"
        if self._same_transport_continuation(in_link, out_link) and abs(angle_deg) <= 35.0:
            return "same_road_continuation"
        if abs(angle_deg) <= 35.0:
            return "straight"
        return "right" if angle_deg < 0.0 else "left"

    def _same_transport_continuation(self, in_link: Link, out_link: Link) -> bool:
        if in_link.metadata.get("osm_direction") != out_link.metadata.get("osm_direction"):
            return False
        if in_link.metadata.get("osm_is_oneway") != out_link.metadata.get("osm_is_oneway"):
            return False
        if in_link.metadata.get("highway") != out_link.metadata.get("highway"):
            return False
        if in_link.parameters.get("lanes_total", 1) != out_link.parameters.get("lanes_total", 1):
            return False
        in_way_id = in_link.metadata.get("osm_way_id")
        if in_way_id and in_way_id == out_link.metadata.get("osm_way_id"):
            return True
        if in_link.metadata.get("osm_name") and in_link.metadata.get("osm_name") == out_link.metadata.get("osm_name"):
            return True
        return in_link.name == out_link.name

    def _validate_movements(self) -> None:
        for node_id, by_in_link in self.movements_by_node.items():
            for in_link_id, movements in by_in_link.items():
                ratio_sum = sum(float(movement["turn_ratio"]) for movement in movements)
                if abs(ratio_sum - 1.0) > self.config.turn_ratio_tolerance:
                    raise CTMStateError(
                        f"invalid movements: ratios for {node_id}/{in_link_id} sum to {ratio_sum:.9f}"
                    )

    def _serialized_movements(self) -> list[dict]:
        serialized = []
        for movement in self.movements:
            public = {
                key: value
                for key, value in movement.items()
                if not key.startswith("_")
            }
            public["turn_ratio"] = round(float(public["turn_ratio"]), 6)
            serialized.append(public)
        return serialized

    def _movement_summary(self) -> dict[str, Any]:
        ratios = [movement["turn_ratio"] for movement in self.movements]
        return {
            "node_solver": NODE_SOLVER_NAME,
            "fifo_strength": self.config.fifo_strength,
            "movement_count": len(self.movements),
            "inferred_count": sum(1 for movement in self.movements if movement["source"] == "inferred"),
            "manual_count": sum(1 for movement in self.movements if movement["source"] == "manual"),
            "short_connector_candidate_count": sum(
                1 for movement in self.movements if "short_connector_candidate" in movement["flags"]
            ),
            "max_turn_ratio": round(max(ratios), 6) if ratios else 0.0,
            "turn_ratio_gt_0_9_count": sum(1 for ratio in ratios if ratio > 0.9),
            "turn_ratio_gt_0_95_count": sum(1 for ratio in ratios if ratio > 0.95),
        }

    def _refresh_movement_metadata(self) -> None:
        self.project.metadata["ctm_movement_warnings"] = list(self.movement_warnings)
        self.project.metadata["ctm_movements"] = self._serialized_movements()
        self.project.metadata["ctm_movement_summary"] = self._movement_summary()

    def _finalize_movement_diagnostics(self) -> None:
        for movement in self.movements:
            sample_count = movement["_flow_sample_count"]
            if sample_count:
                movement["avg_flow_veh_h"] = movement["_flow_sum_veh_h"] / sample_count
            factor_sample_count = movement["_factor_sample_count"]
            if factor_sample_count:
                movement["avg_fifo_factor"] = movement["_fifo_factor_sum"] / factor_sample_count
                movement["avg_nonfifo_factor"] = movement["_nonfifo_factor_sum"] / factor_sample_count
                movement["avg_restriction_factor"] = movement["_restriction_factor_sum"] / factor_sample_count
            movement["avg_flow_veh_h"] = round(movement["avg_flow_veh_h"], 3)
            movement["max_flow_veh_h"] = round(movement["max_flow_veh_h"], 3)
            movement["avg_fifo_factor"] = round(movement["avg_fifo_factor"], 6)
            movement["min_fifo_factor"] = round(movement["min_fifo_factor"], 6)
            movement["avg_nonfifo_factor"] = round(movement["avg_nonfifo_factor"], 6)
            movement["min_nonfifo_factor"] = round(movement["min_nonfifo_factor"], 6)
            movement["avg_restriction_factor"] = round(movement["avg_restriction_factor"], 6)
            movement["min_restriction_factor"] = round(movement["min_restriction_factor"], 6)
        self._refresh_movement_metadata()

    def _init_source_queue_history(self) -> None:
        for source_id in self.sources:
            self.network.links[source_id].results["history_external_queue_pcu"] = []

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
        incident = Incident(
            cell_index=self.incident_cell_index,
            start_time=self.config.incident_start_sec,
            end_time=self.config.incident_end_sec,
            capacity_factor=self.config.incident_capacity_factor,
            speed_factor=self.config.incident_speed_factor,
        )
        ctm.incidents.append(incident)
        incident_data = {
            "cell_index": self.incident_cell_index,
            "start_time_sec": incident.start_time,
            "end_time_sec": incident.end_time,
            "capacity_factor": incident.capacity_factor,
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
            f"cell {self.incident_cell_index}, {incident.start_time:.0f}-{incident.end_time:.0f}s."
        )

    def _calc_turn_angle(self, in_link: Link, out_link: Link) -> float:
        incoming_points = self._link_lon_lat_points(in_link)
        outgoing_points = self._link_lon_lat_points(out_link)
        if len(incoming_points) < 2 or len(outgoing_points) < 2:
            return 0.0

        in_a, in_b = incoming_points[-2], incoming_points[-1]
        out_a, out_b = outgoing_points[0], outgoing_points[1]
        origin_lat = in_b[1]
        in_a_xy = self._local_xy_m(in_a, origin_lat)
        in_b_xy = self._local_xy_m(in_b, origin_lat)
        out_a_xy = self._local_xy_m(out_a, origin_lat)
        out_b_xy = self._local_xy_m(out_b, origin_lat)

        dx1, dy1 = in_b_xy[0] - in_a_xy[0], in_b_xy[1] - in_a_xy[1]
        dx2, dy2 = out_b_xy[0] - out_a_xy[0], out_b_xy[1] - out_a_xy[1]
        if math.hypot(dx1, dy1) == 0.0 or math.hypot(dx2, dy2) == 0.0:
            return 0.0

        # Positive angle is a left turn in the projected local coordinate system.
        return math.degrees(math.atan2(dx1 * dy2 - dy1 * dx2, dx1 * dx2 + dy1 * dy2))

    def _link_lon_lat_points(self, link: Link) -> list[tuple[float, float]]:
        coords = link.coords or {}
        if coords.get("type") == "polyline" and len(coords.get("points", [])) >= 2:
            return [(float(lon), float(lat)) for lon, lat in coords["points"]]

        if all(key in coords for key in ("lon_start", "lat_start", "lon_end", "lat_end")):
            return [
                (float(coords["lon_start"]), float(coords["lat_start"])),
                (float(coords["lon_end"]), float(coords["lat_end"])),
            ]

        start_node = self.network.nodes.get(link.start_node_id)
        end_node = self.network.nodes.get(link.end_node_id)
        if start_node is None or end_node is None or None in (start_node.lon, start_node.lat, end_node.lon, end_node.lat):
            return []
        return [(float(start_node.lon), float(start_node.lat)), (float(end_node.lon), float(end_node.lat))]

    def _local_xy_m(self, point: tuple[float, float], origin_lat: float) -> tuple[float, float]:
        lon, lat = point
        return (
            math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(origin_lat)),
            math.radians(lat) * EARTH_RADIUS_M,
        )

    def step(self, t_sec: float) -> None:
        for ctm in self.ctm_links.values():
            ctm.apply_incidents()

        demands = {link_id: ctm.demand(ctm.cells[-1]) for link_id, ctm in self.ctm_links.items()}
        supplies = {link_id: ctm.supply(ctm.cells[0]) for link_id, ctm in self.ctm_links.items()}

        actual_inflows = {link_id: 0.0 for link_id in self.ctm_links}
        actual_outflows = {link_id: 0.0 for link_id in self.ctm_links}

        source_rate = self.config.inflow_veh_per_hour / 3600.0
        for source_id in self.sources:
            ctm = self.ctm_links[source_id]
            generated = source_rate * self.dt
            ctm.external_queue += generated
            self.mass_generated += generated

            queued_demand_rate = ctm.external_queue / self.dt
            flow = min(queued_demand_rate, supplies[source_id])
            actual_inflows[source_id] = flow
            admitted = flow * self.dt
            ctm.external_queue = max(0.0, ctm.external_queue - admitted)
            self.mass_entered += admitted

        for sink_id in self.sinks:
            flow = demands[sink_id]
            actual_outflows[sink_id] = flow
            self.mass_exited += flow * self.dt

        self._solve_nodes(demands, supplies, actual_inflows, actual_outflows)

        for link_id, ctm in self.ctm_links.items():
            diagnostics = ctm.step_with_boundary_flows(
                upstream_flow=actual_inflows[link_id],
                downstream_flow=actual_outflows[link_id],
            )
            conservation_error = float(diagnostics["conservation_error_pcu"])
            self.network_conservation_error_pcu += conservation_error
            self.max_abs_link_conservation_error_pcu = max(
                self.max_abs_link_conservation_error_pcu,
                abs(conservation_error),
            )

            if t_sec % self.config.snapshot_interval_sec < self.dt:
                cells_densities = [round(cell.density * 1000, 1) for cell in ctm.cells]
                avg_flow_veh_h = actual_outflows[link_id] * 3600.0

                link = self.project.network.links[link_id]
                link.results["history_cells_density_pcu_km"].append(cells_densities)
                link.results["history_flow_veh_h"].append(round(avg_flow_veh_h, 1))
                if link_id in self.sources:
                    link.results["history_external_queue_pcu"].append(round(ctm.external_queue, 3))

    def _solve_nodes(
        self,
        demands: dict[str, float],
        supplies: dict[str, float],
        actual_inflows: dict[str, float],
        actual_outflows: dict[str, float],
    ) -> None:
        for movements_by_in_link in self.movements_by_node.values():
            outlink_total_demand = defaultdict(float)
            desired_by_movement = {}
            nonfifo_factor_by_movement = {}
            fifo_factor_by_in_link = {}

            for in_id, movements in movements_by_in_link.items():
                for movement in movements:
                    out_id = movement["out_link_id"]
                    desired_flow = demands[in_id] * movement["turn_ratio"]
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
                    if (
                        has_desired_flow
                        and fifo_factor < nonfifo_factor - EPS
                    ):
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

    def _record_movement_step(
        self,
        movement: dict,
        actual_flow: float,
        desired_flow: float,
        fifo_factor: float,
        nonfifo_factor: float,
        restriction_factor: float,
    ) -> None:
        flow_veh_h = actual_flow * 3600.0
        movement["_flow_sum_veh_h"] += flow_veh_h
        movement["_flow_sample_count"] += 1
        movement["max_flow_veh_h"] = max(movement["max_flow_veh_h"], flow_veh_h)
        if desired_flow > EPS:
            movement["_factor_sample_count"] += 1
            movement["_fifo_factor_sum"] += fifo_factor
            movement["_nonfifo_factor_sum"] += nonfifo_factor
            movement["_restriction_factor_sum"] += restriction_factor
            movement["min_fifo_factor"] = min(movement["min_fifo_factor"], fifo_factor)
            movement["min_nonfifo_factor"] = min(movement["min_nonfifo_factor"], nonfifo_factor)
            movement["min_restriction_factor"] = min(movement["min_restriction_factor"], restriction_factor)

    def run(self) -> None:
        total_steps = int((self.config.simulation_minutes * 60) / self.dt)
        print(f"Simulation started. Steps: {total_steps}.")

        for step in range(total_steps):
            t_sec = step * self.dt
            self.step(t_sec)

            if step > 0 and step % int(300 / self.dt) == 0:
                print(f"Model time: {int(t_sec / 60)} min.")

        mass_in_network = sum(
            cell.density * cell.length for ctm in self.ctm_links.values() for cell in ctm.cells
        )
        external_queue = sum(self.ctm_links[source_id].external_queue for source_id in self.sources)
        network_error = self.mass_entered - self.mass_exited - mass_in_network
        source_queue_error = self.mass_generated - self.mass_entered - external_queue
        demand_balance_error = (
            self.mass_generated
            - self.mass_exited
            - mass_in_network
            - external_queue
        )
        self._finalize_movement_diagnostics()
        tolerance = 1e-6 * total_steps
        self.project.metadata["ctm_simulation"] = {
            "dt_seconds": self.dt,
            "simulation_minutes": self.config.simulation_minutes,
            "cell_length_target_m": self.config.cell_length_target_m,
            "strict": True,
            "validate_cfl": True,
            "node_solver": NODE_SOLVER_NAME,
            "fifo_strength": self.config.fifo_strength,
            "total_generated_pcu": self.mass_generated,
            "total_entered_pcu": self.mass_entered,
            "total_exited_pcu": self.mass_exited,
            "mass_in_network_pcu": mass_in_network,
            "total_external_queue_pcu": external_queue,
            "conservation_error_pcu": network_error,
            "source_queue_balance_error_pcu": source_queue_error,
            "demand_balance_error_pcu": demand_balance_error,
            "sum_link_conservation_error_pcu": self.network_conservation_error_pcu,
            "max_abs_link_conservation_error_pcu": self.max_abs_link_conservation_error_pcu,
        }

        print("\n=== CTM mass balance ===")
        print(f"Generated demand: {self.mass_generated:.2f}")
        print(f"Entered network: {self.mass_entered:.2f}")
        print(f"Exited network: {self.mass_exited:.2f}")
        print(f"Mass in network: {mass_in_network:.2f}")
        print(f"External source queues: {external_queue:.2f}")
        print(f"Network balance error: {network_error:.6f}")
        print(f"Source queue balance error: {source_queue_error:.6f}")
        print(f"Full demand balance error: {demand_balance_error:.6f}")
        print("========================\n")
        if (
            abs(network_error) > tolerance
            or abs(source_queue_error) > tolerance
            or abs(demand_balance_error) > tolerance
            or abs(self.network_conservation_error_pcu) > tolerance
        ):
            raise CTMStateError(
                "network conservation check failed: "
                f"network error={network_error:.9f}, "
                f"source queue error={source_queue_error:.9f}, "
                f"demand balance error={demand_balance_error:.9f}, "
                f"sum link error={self.network_conservation_error_pcu:.9f}, "
                f"tolerance={tolerance:.9f}"
            )


if __name__ == "__main__":
    input_file = "osm_network_project_map_nstu.json"
    output_file = "ctm_results_viz.json"

    project = ProjectLoader().load(input_file)
    simulator = CTMSimulator(project, CTMScenarioConfig(fifo_strength=1.0, incident_link_id="L1"))
    simulator.run()

    ProjectSaver().save(project, output_file)
    print(f"Results saved to {output_file}.")
