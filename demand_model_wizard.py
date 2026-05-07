from __future__ import annotations

import heapq
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
)

from models import Link, Network, Project
from project_loader import ProjectLoader
from project_saver import ProjectSaver


IMPORTANT_HIGHWAYS = {"primary", "secondary", "tertiary"}
DEFAULT_BOUNDARY_MARGIN_PERCENT = 7
DEFAULT_MAX_DESTINATIONS_PER_ORIGIN = 3


@dataclass
class AutoDemandBuildResult:
    boundary_nodes: list[str]
    intersections: list[str]
    roundabout_parts: list[str]
    routes: list[dict[str, Any]]
    warnings: list[str]


class AutoDemandBuilder:
    """
    Builds a first draft local demand model automatically.

    The builder is deliberately conservative: it does not claim to infer real OD demand.
    It only finds candidate boundary nodes near the imported map border and builds shortest
    directed routes between them. Route split coefficients are distributed uniformly per
    origin. A user can later replace boundary flows and coefficients with real SKDF/field
    values.
    """

    def __init__(
        self,
        default_boundary_flow: float = 1200.0,
        boundary_margin_percent: int = DEFAULT_BOUNDARY_MARGIN_PERCENT,
        max_destinations_per_origin: int = DEFAULT_MAX_DESTINATIONS_PER_ORIGIN,
        include_residential_boundaries: bool = False,
    ) -> None:
        self.default_boundary_flow = float(default_boundary_flow)
        self.boundary_margin_percent = boundary_margin_percent
        self.max_destinations_per_origin = max_destinations_per_origin
        self.include_residential_boundaries = include_residential_boundaries

    def apply(self, project: Project) -> AutoDemandBuildResult:
        network = project.network
        warnings: list[str] = []

        self._classify_nodes(network)
        boundary_nodes = self._detect_boundary_nodes(network)
        if len(boundary_nodes) < 2:
            warnings.append(
                "Detected fewer than two boundary nodes. Increase margin or include residential boundaries."
            )

        routes = self._build_route_splits(network, boundary_nodes, warnings)
        if not routes:
            warnings.append(
                "No boundary-to-boundary routes were built. Check link directions or regenerate project with two-way links."
            )

        project.demand_model = {
            "type": "route_split_coefficients",
            "unit": "veh/h",
            "description": (
                "Auto-generated draft: boundary flows are placeholders, route split coefficients "
                "are uniform over shortest paths. Replace them with observed/SKDF/scenario values."
            ),
            "boundary_flows": {node_id: self.default_boundary_flow for node_id in boundary_nodes},
            "route_split_coefficients": routes,
            "metadata": {
                "generated_by": "DemandModelWizard",
                "boundary_margin_percent": self.boundary_margin_percent,
                "max_destinations_per_origin": self.max_destinations_per_origin,
                "include_residential_boundaries": self.include_residential_boundaries,
            },
        }
        project.metadata.setdefault("demand_model_notes", []).append(
            "Auto-generated demand model is a draft. Boundary flows and split coefficients are placeholders."
        )
        return AutoDemandBuildResult(
            boundary_nodes=boundary_nodes,
            intersections=[node_id for node_id, node in network.nodes.items() if node.node_type == "intersection"],
            roundabout_parts=[node_id for node_id, node in network.nodes.items() if node.node_type == "roundabout_part"],
            routes=routes,
            warnings=warnings,
        )

    def _classify_nodes(self, network: Network) -> None:
        incoming = self._incoming(network)
        outgoing = self._outgoing(network)
        for node_id, node in network.nodes.items():
            in_degree = len(incoming.get(node_id, []))
            out_degree = len(outgoing.get(node_id, []))
            degree = len({link.id for link in incoming.get(node_id, []) + outgoing.get(node_id, [])})
            node.metadata["in_degree"] = in_degree
            node.metadata["out_degree"] = out_degree
            node.metadata["degree"] = degree
            node.metadata["incident_highways"] = sorted(
                {
                    str(link.metadata.get("highway", ""))
                    for link in incoming.get(node_id, []) + outgoing.get(node_id, [])
                    if link.metadata.get("highway")
                }
            )

            if self._looks_like_roundabout_part(network, node_id, incoming, outgoing):
                node.node_type = "roundabout_part"
            elif degree >= 3:
                node.node_type = "intersection"
            elif node.node_type not in {"boundary", "roundabout_part"}:
                node.node_type = "ordinary"

    def _detect_boundary_nodes(self, network: Network) -> list[str]:
        nodes_with_coords = [node for node in network.nodes.values() if node.lon is not None and node.lat is not None]
        if not nodes_with_coords:
            return []

        lons = [float(node.lon) for node in nodes_with_coords]
        lats = [float(node.lat) for node in nodes_with_coords]
        lon_min, lon_max = min(lons), max(lons)
        lat_min, lat_max = min(lats), max(lats)
        lon_margin = max((lon_max - lon_min) * self.boundary_margin_percent / 100.0, 1e-9)
        lat_margin = max((lat_max - lat_min) * self.boundary_margin_percent / 100.0, 1e-9)

        incoming = self._incoming(network)
        outgoing = self._outgoing(network)
        candidates: list[tuple[int, str]] = []
        for node in nodes_with_coords:
            incident = incoming.get(node.id, []) + outgoing.get(node.id, [])
            if not incident:
                continue
            if not self.include_residential_boundaries and not self._has_important_incident_highway(incident):
                continue
            near_border = (
                float(node.lon) <= lon_min + lon_margin
                or float(node.lon) >= lon_max - lon_margin
                or float(node.lat) <= lat_min + lat_margin
                or float(node.lat) >= lat_max - lat_margin
            )
            if not near_border:
                continue
            # Prefer true network stubs first, then other border nodes.
            degree = int(node.metadata.get("degree", len({link.id for link in incident})))
            score = 0 if degree <= 2 else 1
            candidates.append((score, node.id))

        boundary_nodes = [node_id for _, node_id in sorted(candidates)]
        for node_id in boundary_nodes:
            network.nodes[node_id].node_type = "boundary"
            network.nodes[node_id].metadata["boundary_candidate"] = True
        return boundary_nodes

    def _build_route_splits(
        self,
        network: Network,
        boundary_nodes: list[str],
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        route_splits: list[dict[str, Any]] = []
        for origin in boundary_nodes:
            destinations = []
            for destination in boundary_nodes:
                if destination == origin:
                    continue
                path, length = self._shortest_path(network, origin, destination)
                if path:
                    destinations.append((length, destination, path))
            destinations.sort(key=lambda item: item[0])
            destinations = destinations[: self.max_destinations_per_origin]
            if not destinations:
                warnings.append(f"No outgoing route from boundary node {origin}.")
                continue
            coefficient = 1.0 / len(destinations)
            for _, destination, path in destinations:
                route_splits.append(
                    {
                        "id": f"RS_{self._safe_id(origin)}_{self._safe_id(destination)}",
                        "from": origin,
                        "to": destination,
                        "coefficient": round(coefficient, 6),
                        "vehicle_type": "car",
                        "link_ids": path,
                    }
                )
        return route_splits

    def _shortest_path(self, network: Network, origin: str, destination: str) -> tuple[list[str], float]:
        outgoing = self._outgoing(network)
        queue: list[tuple[float, str, list[str]]] = [(0.0, origin, [])]
        best: dict[str, float] = {origin: 0.0}
        while queue:
            cost, node_id, path = heapq.heappop(queue)
            if node_id == destination:
                return path, cost
            if cost > best.get(node_id, float("inf")):
                continue
            for link in outgoing.get(node_id, []):
                if link.metadata.get("disabled"):
                    continue
                next_node = link.end_node_id
                next_cost = cost + max(float(link.length_km or 0.001), 0.001)
                if next_cost < best.get(next_node, float("inf")):
                    best[next_node] = next_cost
                    heapq.heappush(queue, (next_cost, next_node, path + [link.id]))
        return [], float("inf")

    def _looks_like_roundabout_part(
        self,
        network: Network,
        node_id: str,
        incoming: dict[str, list[Link]],
        outgoing: dict[str, list[Link]],
    ) -> bool:
        incident = incoming.get(node_id, []) + outgoing.get(node_id, [])
        if len({link.id for link in incident}) != 2:
            return False
        names = " ".join((link.name or "").lower() for link in incident)
        if "кольц" in names or "roundabout" in names:
            return True
        if any(str(link.metadata.get("junction", "")).lower() == "roundabout" for link in incident):
            return True
        return all(float(link.length_km or 0.0) <= 0.12 for link in incident) and self._local_cluster_is_dense(network, node_id)

    def _local_cluster_is_dense(self, network: Network, node_id: str) -> bool:
        node = network.nodes[node_id]
        if node.lon is None or node.lat is None:
            return False
        nearby = 0
        for other in network.nodes.values():
            if other.id == node_id or other.lon is None or other.lat is None:
                continue
            if _haversine_km(float(node.lon), float(node.lat), float(other.lon), float(other.lat)) <= 0.18:
                nearby += 1
        return nearby >= 4

    def _has_important_incident_highway(self, incident: list[Link]) -> bool:
        return any(str(link.metadata.get("highway", "")) in IMPORTANT_HIGHWAYS for link in incident)

    def _incoming(self, network: Network) -> dict[str, list[Link]]:
        result: dict[str, list[Link]] = defaultdict(list)
        for link in network.links.values():
            result[link.end_node_id].append(link)
        return result

    def _outgoing(self, network: Network) -> dict[str, list[Link]]:
        result: dict[str, list[Link]] = defaultdict(list)
        for link in network.links.values():
            result[link.start_node_id].append(link)
        return result

    def _safe_id(self, value: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in value)[-32:]


class DemandModelWizard(QDialog):
    def __init__(self, project_file: str, parent=None):
        super().__init__(parent)
        self.project_file = project_file
        self.project: Project | None = None
        self.last_result: AutoDemandBuildResult | None = None
        self.setWindowTitle("Автогенерация demand_model")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Файл проекта: {project_file}"))

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Граничный поток, авт/ч:"))
        self.flow_spin = QDoubleSpinBox()
        self.flow_spin.setRange(0, 100000)
        self.flow_spin.setValue(1200)
        self.flow_spin.setDecimals(0)
        controls.addWidget(self.flow_spin)

        controls.addWidget(QLabel("Край карты, %:"))
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(1, 30)
        self.margin_spin.setValue(DEFAULT_BOUNDARY_MARGIN_PERCENT)
        controls.addWidget(self.margin_spin)

        controls.addWidget(QLabel("Выходов на вход:"))
        self.max_dest_spin = QSpinBox()
        self.max_dest_spin.setRange(1, 10)
        self.max_dest_spin.setValue(DEFAULT_MAX_DESTINATIONS_PER_ORIGIN)
        controls.addWidget(self.max_dest_spin)
        layout.addLayout(controls)

        self.include_residential = QCheckBox("Считать residential на границе входами/выходами")
        self.include_residential.setChecked(False)
        layout.addWidget(self.include_residential)

        buttons = QHBoxLayout()
        self.btn_generate = QPushButton("Автоматически построить demand_model")
        self.btn_generate.clicked.connect(self.generate)
        buttons.addWidget(self.btn_generate)
        self.btn_save = QPushButton("Сохранить в JSON")
        self.btn_save.clicked.connect(self.save)
        self.btn_save.setEnabled(False)
        buttons.addWidget(self.btn_save)
        layout.addLayout(buttons)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        layout.addWidget(self.preview)

        self.load_project()

    def load_project(self) -> None:
        try:
            self.project = ProjectLoader().load(self.project_file)
        except Exception as exc:
            QMessageBox.critical(self, "Demand model", f"Не удалось загрузить проект:\n{exc}")
            self.project = None
            return
        self.preview.setPlainText(
            f"Загружено: {self.project.project_name}\n"
            f"nodes: {len(self.project.network.nodes)}\n"
            f"links: {len(self.project.network.links)}\n\n"
            "Нажми кнопку автогенерации. После этого проверь список boundary nodes/routes и сохрани."
        )

    def generate(self) -> None:
        if self.project is None:
            return
        builder = AutoDemandBuilder(
            default_boundary_flow=self.flow_spin.value(),
            boundary_margin_percent=self.margin_spin.value(),
            max_destinations_per_origin=self.max_dest_spin.value(),
            include_residential_boundaries=self.include_residential.isChecked(),
        )
        self.last_result = builder.apply(self.project)
        self.btn_save.setEnabled(True)
        self.preview.setPlainText(self._format_result(self.last_result))
        if self.last_result.warnings:
            QMessageBox.warning(self, "Demand model warnings", "\n".join(self.last_result.warnings[:12]))

    def save(self) -> None:
        if self.project is None or self.last_result is None:
            return
        try:
            ProjectSaver().save(self.project, self.project_file)
        except Exception as exc:
            QMessageBox.critical(self, "Demand model", f"Не удалось сохранить проект:\n{exc}")
            return
        QMessageBox.information(self, "Demand model", f"demand_model сохранён в {self.project_file}")
        self.accept()

    def _format_result(self, result: AutoDemandBuildResult) -> str:
        lines = [
            "Auto demand_model draft",
            f"boundary nodes: {len(result.boundary_nodes)}",
            f"intersections: {len(result.intersections)}",
            f"roundabout parts: {len(result.roundabout_parts)}",
            f"route splits: {len(result.routes)}",
            "",
            "Boundary nodes:",
        ]
        for node_id in result.boundary_nodes[:80]:
            node = self.project.network.nodes[node_id]
            lines.append(
                f"- {node_id} | {node.name or node_id} | type={node.node_type} | "
                f"degree={node.metadata.get('degree')} | highways={node.metadata.get('incident_highways')}"
            )
        if len(result.boundary_nodes) > 80:
            lines.append(f"... {len(result.boundary_nodes) - 80} more")
        lines.append("")
        lines.append("Routes:")
        for route in result.routes[:120]:
            lines.append(
                f"- {route['id']}: {route['from']} -> {route['to']} | "
                f"coef={route['coefficient']} | links={len(route['link_ids'])}"
            )
        if len(result.routes) > 120:
            lines.append(f"... {len(result.routes) - 120} more")
        if result.warnings:
            lines.append("")
            lines.append("Warnings:")
            lines.extend(f"- {warning}" for warning in result.warnings)
        lines.append("")
        lines.append(
            "Важно: это черновик. Потоки и коэффициенты равномерные/заглушечные. "
            "Для диплома их надо объяснить как сценарные значения или заменить данными."
        )
        return "\n".join(lines)


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(d_lon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))
