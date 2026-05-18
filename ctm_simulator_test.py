from __future__ import annotations
import math
from collections import defaultdict

from models import Project, Link
from project_loader import ProjectLoader
from project_saver import ProjectSaver

# Импортируем ядро CTM
from ctm_network_core_v2 import (
    CTMModel,
    CTMStateError,
    Incident,
    TriangularFundamentalDiagram,
)

# --- НАСТРОЙКИ СИМУЛЯЦИИ ---
DT_SECONDS = 0.5  # Шаг времени (0.5 сек - безопасно для коротких OSM сегментов)
SIMULATION_MINUTES = 100
SNAPSHOT_INTERVAL_SEC = 60  # Шаг ползунка времени (1 минута)
CELL_LENGTH_TARGET = 15.0  # Желаемая длина ячейки (15 метров)

# Снизили входящий поток, чтобы сеть не захлебывалась на ровном месте
INFLOW_VEH_PER_HOUR = 475.0

# Пропускные способности (подогнаны под реалистичные городские 1-полосные улицы)
HIGHWAY_PARAMS = {
    "primary": {"speed_kph": 60, "cap_per_lane": 1000, "weight": 3.0},
    "secondary": {"speed_kph": 50, "cap_per_lane": 800, "weight": 2.0},
    "tertiary": {"speed_kph": 40, "cap_per_lane": 700, "weight": 1.0},
    "residential": {"speed_kph": 30, "cap_per_lane": 500, "weight": 0.5},
    "trunk": {"speed_kph": 80, "cap_per_lane": 1400, "weight": 4.0},
    "default": {"speed_kph": 40, "cap_per_lane": 600, "weight": 1.0},
}

TURN_WEIGHTS = {
    "same_road_continuation": 3.0,
    "straight": 2.0,
    "right": 0.7,
    "left": 0.45,
    "u_turn": 0.0,
}
SHORT_CONNECTOR_LENGTH_M = 30.0
TURN_RATIO_TOLERANCE = 1e-6
EARTH_RADIUS_M = 6371008.8


class CTMSimulator:
    def __init__(self, project: Project):
        self.project = project
        self.network = project.network
        self.dt = DT_SECONDS

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

        self._init_physics()
        self._build_movements()
        self._init_source_queue_history()
        self._plan_incident()

    def _init_physics(self):
        print("Инициализация CTM-моделей (нарезка ячеек)...")
        for link in self.network.links.values():
            hw = link.metadata.get("highway", "default")
            params = HIGHWAY_PARAMS.get(hw, HIGHWAY_PARAMS["default"])
            lanes = link.parameters.get("lanes_total", 1)

            diagram = TriangularFundamentalDiagram.from_common_units(
                free_flow_speed_kph=params["speed_kph"],
                backward_wave_speed_kph=18.0,
                capacity_pcu_h=params["cap_per_lane"] * lanes,
                jam_density_pcu_km=140.0 * lanes
            )

            # Защита от вылета (CFL): минимальная длина линка в расчете = 10 метров
            max_wave_speed = max(diagram.free_flow_speed, diagram.backward_wave_speed)
            min_cfl_cell_length = self.dt * max_wave_speed
            cell_length_target = max(CELL_LENGTH_TARGET, min_cfl_cell_length)
            length_m = max(link.length_km * 1000.0, min_cfl_cell_length)
            cell_count = max(1, math.floor(length_m / cell_length_target))

            ctm = CTMModel.create_uniform_link(
                length=length_m,
                cell_length=length_m / cell_count,
                diagram=diagram,
                dt=self.dt,
                validate_cfl=True
            )

            # Массивы для поклеточной истории
            link.results = {
                "cell_count": cell_count,
                "history_cells_density_pcu_km": [],
                "history_flow_veh_h": [],
                "ctm_length_m": round(length_m, 3),
                "ctm_cell_length_m": round(length_m / cell_count, 3),
            }
            self.ctm_links[link.id] = ctm

    def _build_turning_ratios_legacy_unused(self):
        print("Анализ топологии узлов...")
        for node_id, node in self.network.nodes.items():
            incoming = self.network.get_incoming_links(node_id)
            outgoing = self.network.get_outgoing_links(node_id)

            neighbors = {l.start_node_id for l in incoming} | {l.end_node_id for l in outgoing}
            node_type = getattr(node, "node_type", "") or ""

            # Тупики (границы карты)
            if node_type == "boundary" or len(neighbors) <= 1:
                self.sources.extend([l.id for l in outgoing])
                self.sinks.extend([l.id for l in incoming])
                continue

            if node_type and node_type != "intersection":
                self._add_forced_through_movements(node_id, incoming, outgoing)
                continue

            # Перекрестки
            for in_link in incoming:
                scores, total_score = {}, 0.0
                for out_link in outgoing:
                    if out_link.end_node_id == in_link.start_node_id: continue
                    angle_deg = self._calc_turn_angle(in_link, out_link)
                    if abs(angle_deg) < 35:
                        turn_weight = 1.0
                    elif angle_deg < 0:
                        turn_weight = 0.3
                    else:
                        turn_weight = 0.1

                    hw = out_link.metadata.get("highway", "default")
                    hw_weight = HIGHWAY_PARAMS.get(hw, HIGHWAY_PARAMS["default"])["weight"]
                    lanes = out_link.parameters.get("lanes_total", 1)

                    score = turn_weight * hw_weight * lanes
                    scores[out_link.id] = score
                    total_score += score

                if total_score > 0:
                    for out_id, score in scores.items():
                        self.turn_ratios[node_id][in_link.id][out_id] = score / total_score
        print(f"Генераторов трафика: {len(self.sources)}, Стоков: {len(self.sinks)}")

    def _add_forced_through_movements(self, node_id: str, incoming: list[Link], outgoing: list[Link]) -> None:
        for in_link in incoming:
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
            if len(selected) == 1:
                self.turn_ratios[node_id][in_link.id][selected[0].id] = 1.0

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

    def _build_movements(self):
        print("РђРЅР°Р»РёР· С‚РѕРїРѕР»РѕРіРёРё СѓР·Р»РѕРІ...")
        overrides = self.project.metadata.setdefault("turn_ratio_overrides", {}) or {}
        for node_id, node in self.network.nodes.items():
            incoming = self.network.get_incoming_links(node_id)
            outgoing = self.network.get_outgoing_links(node_id)

            neighbors = {l.start_node_id for l in incoming} | {l.end_node_id for l in outgoing}
            node_type = getattr(node, "node_type", "") or ""

            if node_type == "boundary" or len(neighbors) <= 1:
                self.sources.extend([l.id for l in outgoing])
                self.sinks.extend([l.id for l in incoming])
                continue

            for in_link in incoming:
                if self._apply_manual_override(node_id, in_link, outgoing, overrides):
                    continue
                if node_type and node_type != "intersection":
                    self._add_forced_through_movement(node_id, in_link, outgoing, node_type)
                else:
                    self._add_inferred_movements(node_id, in_link, outgoing)

        self._validate_movements()
        self.project.metadata["ctm_movement_warnings"] = list(self.movement_warnings)
        self.project.metadata["ctm_movements"] = self._serialized_movements()
        print(f"Р“РµРЅРµСЂР°С‚РѕСЂРѕРІ С‚СЂР°С„РёРєР°: {len(self.sources)}, РЎС‚РѕРєРѕРІ: {len(self.sinks)}")

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

        if abs(ratio_sum - 1.0) > TURN_RATIO_TOLERANCE:
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
            "_flow_sum_veh_h": 0.0,
            "_flow_sample_count": 0,
        }
        self.movements.append(movement)
        self.movements_by_node[node_id][in_link.id].append(movement)
        self.turn_ratios[node_id][in_link.id][out_link.id] = float(turn_ratio)

    def _movement_score(self, in_link: Link, out_link: Link, turn_type: str) -> tuple[float, list[str], list[str]]:
        reason = [turn_type]
        flags = self._movement_flags(in_link, out_link, turn_type)
        score = TURN_WEIGHTS[turn_type]

        if "same_osm_way_id" in flags:
            score *= 3.0
            reason.append("same_osm_way_id")
        elif "same_osm_name" in flags:
            score *= 2.0
            reason.append("same_osm_name")
        elif "same_visible_name" in flags:
            score *= 1.5
            reason.append("same_visible_name")
        elif "same_highway_straight" in flags:
            score *= 1.0
            reason.append("same_highway_straight")

        hw = out_link.metadata.get("highway", "default")
        score *= HIGHWAY_PARAMS.get(hw, HIGHWAY_PARAMS["default"])["weight"]
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
        if link.length_km * 1000.0 >= SHORT_CONNECTOR_LENGTH_M:
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
                if abs(ratio_sum - 1.0) > TURN_RATIO_TOLERANCE:
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

    def _finalize_movement_diagnostics(self) -> None:
        for movement in self.movements:
            sample_count = movement["_flow_sample_count"]
            if sample_count:
                movement["avg_flow_veh_h"] = movement["_flow_sum_veh_h"] / sample_count
            movement["avg_flow_veh_h"] = round(movement["avg_flow_veh_h"], 3)
            movement["max_flow_veh_h"] = round(movement["max_flow_veh_h"], 3)
        self.project.metadata["ctm_movements"] = self._serialized_movements()

    def _init_source_queue_history(self):
        for source_id in self.sources:
            self.network.links[source_id].results["history_external_queue_pcu"] = []

    def _plan_incident(self):
        """Находит самую длинную внутреннюю дорогу и планирует аварию в её ЦЕНТРАЛЬНОЙ ячейке."""
        candidates = [
            l for l in self.network.links.values()
            if l.id not in self.sources and l.id not in self.sinks
        ]
        if candidates:
            longest = max(candidates, key=lambda x: x.length_km)
            self.incident_link_id = longest.id
            self.incident_link_id = "L1"

            # Находим центральную ячейку
            ctm = self.ctm_links[self.incident_link_id]
            self.incident_cell_index = ctm.cell_count // 2
            incident = Incident(
                cell_index=self.incident_cell_index,
                start_time=300.0,
                end_time=900.0,
                capacity_factor=0.1,
                speed_factor=1.0,
            )
            ctm.incidents.append(incident)
            longest.results["incident"] = {
                "cell_index": self.incident_cell_index,
                "start_time_sec": incident.start_time,
                "end_time_sec": incident.end_time,
                "capacity_factor": incident.capacity_factor,
                "speed_factor": incident.speed_factor,
            }
            self.project.metadata["ctm_incident"] = {
                "link_id": longest.id,
                "link_name": longest.name,
                "cell_index": self.incident_cell_index,
                "start_time_sec": incident.start_time,
                "end_time_sec": incident.end_time,
                "capacity_factor": incident.capacity_factor,
                "speed_factor": incident.speed_factor,
            }

            print(f"ВНИМАНИЕ: Запланирована авария на дороге {longest.id} ({longest.name}).")
            print(f"Длина: {longest.length_km * 1000:.1f} м. Ячеек: {ctm.cell_count}.")
            print(f"Авария произойдет только в ячейке № {self.incident_cell_index} (с 5 по 15 минуту).")

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

    def step(self, t_sec: float):
        for ctm in self.ctm_links.values():
            ctm.apply_incidents()

        # 1. Опрос желаний
        demands = {l_id: ctm.demand(ctm.cells[-1]) for l_id, ctm in self.ctm_links.items()}
        supplies = {l_id: ctm.supply(ctm.cells[0]) for l_id, ctm in self.ctm_links.items()}

        actual_inflows = {l: 0.0 for l in self.ctm_links}
        actual_outflows = {l: 0.0 for l in self.ctm_links}

        # 2. Обработка краев сети
        source_rate = INFLOW_VEH_PER_HOUR / 3600.0
        for s_id in self.sources:
            ctm = self.ctm_links[s_id]
            generated = source_rate * self.dt
            ctm.external_queue += generated
            self.mass_generated += generated

            queued_demand_rate = ctm.external_queue / self.dt
            flow = min(queued_demand_rate, supplies[s_id])
            actual_inflows[s_id] = flow
            admitted = flow * self.dt
            ctm.external_queue = max(0.0, ctm.external_queue - admitted)
            self.mass_entered += admitted

        for s_id in self.sinks:
            flow = demands[s_id]
            actual_outflows[s_id] = flow
            self.mass_exited += flow * self.dt

        # 3. Перекрестки (Node Solver - Proportional split)
        for node_id, movements_by_in_link in self.movements_by_node.items():
            outlink_total_demand = defaultdict(float)
            desired_by_movement = {}
            for in_id, movements in movements_by_in_link.items():
                for movement in movements:
                    out_id = movement["out_link_id"]
                    desired_flow = demands[in_id] * movement["turn_ratio"]
                    desired_by_movement[id(movement)] = desired_flow
                    outlink_total_demand[out_id] += desired_flow

            for in_id, movements in movements_by_in_link.items():
                for movement in movements:
                    out_id = movement["out_link_id"]
                    desired_flow = desired_by_movement[id(movement)]
                    total_demand = outlink_total_demand[out_id]
                    supply = supplies[out_id]

                    if total_demand > supply:
                        actual_flow = desired_flow * (supply / total_demand)
                        movement["blocked_by_supply_count"] += 1
                    else:
                        actual_flow = desired_flow

                    actual_inflows[out_id] += actual_flow
                    actual_outflows[in_id] += actual_flow
                    flow_veh_h = actual_flow * 3600.0
                    movement["_flow_sum_veh_h"] += flow_veh_h
                    movement["_flow_sample_count"] += 1
                    movement["max_flow_veh_h"] = max(movement["max_flow_veh_h"], flow_veh_h)

        # 4. Внутреннее движение машин (Уравнение LWR)
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

            # 5. Сбор поклеточной истории (Снапшот раз в минуту)
            if t_sec % SNAPSHOT_INTERVAL_SEC < self.dt:
                # Массив плотностей для каждой ячейки этой дороги!
                cells_densities = [round(c.density * 1000, 1) for c in ctm.cells]
                avg_flow_veh_h = actual_outflows[link_id] * 3600.0

                link = self.project.network.links[link_id]
                link.results["history_cells_density_pcu_km"].append(cells_densities)
                link.results["history_flow_veh_h"].append(round(avg_flow_veh_h, 1))
                if link_id in self.sources:
                    link.results["history_external_queue_pcu"].append(
                        round(ctm.external_queue, 3)
                    )

    def run(self):
        total_steps = int((SIMULATION_MINUTES * 60) / self.dt)
        print(f"Симуляция начата. Шагов: {total_steps}...")

        for step in range(total_steps):
            t_sec = step * self.dt
            self.step(t_sec)

            if step > 0 and step % int(300 / self.dt) == 0:
                print(f"Модельное время: {int(t_sec / 60)} мин...")

        # Проверка баланса массы
        mass_in_network = sum(
            c.density * c.length for ctm in self.ctm_links.values() for c in ctm.cells
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
        print("\n=== ДОКАЗАТЕЛЬСТВО ЗАКОНА СОХРАНЕНИЯ МАССЫ ===")
        print(f"Сгенерированный входной спрос: {self.mass_generated:.2f}")
        print(f"Машин въехало в сеть: {self.mass_entered:.2f}")
        print(f"Машин выехало: {self.mass_exited:.2f}")
        print(f"Осталось в пробках и на дорогах: {mass_in_network:.2f}")
        print(f"Осталось во внешних очередях источников: {external_queue:.2f}")
        self._finalize_movement_diagnostics()
        tolerance = 1e-6 * total_steps
        self.project.metadata["ctm_simulation"] = {
            "dt_seconds": self.dt,
            "simulation_minutes": SIMULATION_MINUTES,
            "cell_length_target_m": CELL_LENGTH_TARGET,
            "strict": True,
            "validate_cfl": True,
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
        print(f"Погрешность сетевого баланса entered = exited + network: {network_error:.6f}")
        print(f"Погрешность входного баланса generated = entered + queue: {source_queue_error:.6f}")
        print(f"Погрешность полного баланса generated = exited + network + queue: {demand_balance_error:.6f}")
        print("==============================================\n")
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
    simulator = CTMSimulator(project)
    simulator.run()

    ProjectSaver().save(project, output_file)
    print(f"Результаты сохранены в {output_file}.")
