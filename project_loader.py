from __future__ import annotations

import json
from pathlib import Path

from models import Link, Network, Node, Project, Route, Scenario


class ProjectLoader:
    def load(self, path: str | Path) -> Project:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if "network" in data:
            return self._load_modern_project(data)
        return self._load_legacy_project(data)

    def _load_modern_project(self, data: dict) -> Project:
        project = Project(
            project_name=data.get("project_name", "Unnamed Project"),
            pcu_coefficients=data.get("pcu_coefficients", {}),
            metadata=data.get("metadata", {}),
        )

        network_data = data.get("network", {})
        project.network = self._build_network(network_data)
        project.scenarios = [
            Scenario(
                id=item["id"],
                name=item.get("name", item["id"]),
                description=item.get("description", ""),
                changes=item.get("changes", []),
                results_snapshot=item.get("results_snapshot", {}),
            )
            for item in data.get("scenarios", [])
        ]
        return project

    def _load_legacy_project(self, data: dict) -> Project:
        project = Project(
            project_name=data.get("project_name", "Legacy Project"),
            pcu_coefficients=data.get("pcu_coefficients", {}),
            metadata={"source_format": "legacy"},
        )
        network = Network()

        for item in data.get("directional_links", []):
            start_node_id, end_node_id, coords = self._extract_nodes_and_coords(item)
            for node_id, lon, lat in (
                (start_node_id, coords.get("lon_start"), coords.get("lat_start")),
                (end_node_id, coords.get("lon_end"), coords.get("lat_end")),
            ):
                if node_id not in network.nodes:
                    network.add_node(Node(id=node_id, lon=lon, lat=lat))

            parameters = {
                key: value
                for key, value in item.items()
                if key
                not in {
                    "id",
                    "name",
                    "type",
                    "length_km",
                    "traffic_counts",
                    "coords",
                }
            }

            network.add_link(
                Link(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    start_node_id=start_node_id,
                    end_node_id=end_node_id,
                    link_type=item.get("type", "straight"),
                    length_km=item.get("length_km", 0.0),
                    traffic_counts=item.get("traffic_counts", {}),
                    coords=coords,
                    parameters=parameters,
                )
            )

        for route_data in data.get("routes", []):
            network.add_route(
                Route(
                    id=route_data["id"],
                    name=route_data.get("name", route_data["id"]),
                    link_ids=route_data.get("links", []),
                )
            )

        project.network = network
        return project

    def _build_network(self, network_data: dict) -> Network:
        network = Network()

        for item in network_data.get("nodes", []):
            network.add_node(
                Node(
                    id=item["id"],
                    lon=item.get("lon"),
                    lat=item.get("lat"),
                    x=item.get("x"),
                    y=item.get("y"),
                    node_type=item.get("node_type", "intersection"),
                    name=item.get("name", ""),
                    metadata=item.get("metadata", {}),
                )
            )

        for item in network_data.get("links", []):
            network.add_link(
                Link(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    start_node_id=item["start_node_id"],
                    end_node_id=item["end_node_id"],
                    link_type=item.get("link_type", "straight"),
                    length_km=item.get("length_km", 0.0),
                    traffic_counts=item.get("traffic_counts", {}),
                    coords=item.get("coords", {}),
                    parameters=item.get("parameters", {}),
                    results=item.get("results", {}),
                    metadata=item.get("metadata", {}),
                )
            )

        for item in network_data.get("routes", []):
            network.add_route(
                Route(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    link_ids=item.get("link_ids", []),
                    results=item.get("results", {}),
                )
            )

        return network

    def _extract_nodes_and_coords(self, item: dict) -> tuple[str, str, dict]:
        coords = item.get("coords", {})
        if coords.get("type") == "polyline":
            points = coords.get("points", [])
            if len(points) >= 2:
                coords = {
                    "type": "polyline",
                    "points": points,
                    "lon_start": points[0][0],
                    "lat_start": points[0][1],
                    "lon_end": points[-1][0],
                    "lat_end": points[-1][1],
                }
        start_key = (
            round(coords.get("lon_start", 0.0), 6),
            round(coords.get("lat_start", 0.0), 6),
        )
        end_key = (
            round(coords.get("lon_end", 0.0), 6),
            round(coords.get("lat_end", 0.0), 6),
        )
        return f"N_{start_key[0]}_{start_key[1]}", f"N_{end_key[0]}_{end_key[1]}", coords

