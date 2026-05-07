from __future__ import annotations

import json
from pathlib import Path

from models import Link, Network, Node, Project, Route, Scenario


class ProjectLoader:
    DEFAULT_LEGACY_COORDS = {
        "L5_RING_ENTRY": [82.888, 55.050, 82.890, 55.050],
        "L_RING_CIRCULATION": [82.890, 55.050, 82.892, 55.052],
        "L1_RING_EXIT": [82.892, 55.052, 82.894, 55.054],
        "L2_A_RING_TO_PED": [82.893, 55.050, 82.897, 55.050],
        "L2_PED_SIGNAL": [82.897, 55.050, 82.898, 55.050],
        "L2_B_PED_TO_I3": [82.898, 55.050, 82.902, 55.050],
        "L3_I3_APPROACH": [82.902, 55.050, 82.904, 55.050],
        "L4_A_I3_TO_PED": [82.904, 55.049, 82.902, 55.049],
        "L4_PED_SIGNAL": [82.902, 55.049, 82.898, 55.049],
        "L4_B_PED_TO_RING": [82.898, 55.049, 82.897, 55.049],
    }

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
            demand_model=data.get("demand_model", {}),
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
        coords_source = self._load_legacy_coords_source()
        node_ids_by_coord = {}

        for item in data.get("directional_links", []):
            coords = self._resolve_legacy_coords(item, coords_source)
            start_node_id, end_node_id, coords = self._extract_nodes_and_coords(item, coords, node_ids_by_coord)
            for node_id, lon, lat in (
                (start_node_id, coords.get("lon_start"), coords.get("lat_start")),
                (end_node_id, coords.get("lon_end"), coords.get("lat_end")),
            ):
                if node_id not in network.nodes:
                    network.add_node(Node(id=node_id, lon=lon, lat=lat, name=node_id))

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
                    link_ids=route_data.get("link_ids", route_data.get("links", [])),
                    origin_node_id=route_data.get("origin_node_id", route_data.get("from")),
                    destination_node_id=route_data.get("destination_node_id", route_data.get("to")),
                    demand_value=route_data.get("demand_value"),
                    demand_veh_h=route_data.get("demand_veh_h", route_data.get("demand", 0.0)),
                    vehicle_type=route_data.get("vehicle_type", "car"),
                    results=route_data.get("results", {}),
                    metadata=route_data.get("metadata", {}),
                )
            )

        project.network = network
        self._assign_default_node_names(project.network)
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

        self._backfill_nodes_from_links(network)

        for item in network_data.get("routes", []):
            network.add_route(
                Route(
                    id=item["id"],
                    name=item.get("name", item["id"]),
                    link_ids=item.get("link_ids", item.get("links", [])),
                    origin_node_id=item.get("origin_node_id", item.get("from")),
                    destination_node_id=item.get("destination_node_id", item.get("to")),
                    demand_value=item.get("demand_value"),
                    demand_veh_h=item.get("demand_veh_h", item.get("demand", 0.0)),
                    vehicle_type=item.get("vehicle_type", "car"),
                    results=item.get("results", {}),
                    metadata=item.get("metadata", {}),
                )
            )

        self._assign_default_node_names(network)
        return network

    def _extract_nodes_and_coords(
        self,
        item: dict,
        coords: dict | None = None,
        node_ids_by_coord: dict | None = None,
    ) -> tuple[str, str, dict]:
        coords = coords or item.get("coords", {})
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
        if node_ids_by_coord is None:
            return f"N_{start_key[0]}_{start_key[1]}", f"N_{end_key[0]}_{end_key[1]}", coords

        if start_key not in node_ids_by_coord:
            node_ids_by_coord[start_key] = f"N{len(node_ids_by_coord) + 1}"
        if end_key not in node_ids_by_coord:
            node_ids_by_coord[end_key] = f"N{len(node_ids_by_coord) + 1}"

        return node_ids_by_coord[start_key], node_ids_by_coord[end_key], coords

    def _load_legacy_coords_source(self) -> dict:
        coords_source = {}
        saved_positions_path = Path("saved_positions.json")
        if saved_positions_path.exists():
            try:
                with open(saved_positions_path, "r", encoding="utf-8") as f:
                    coords_source.update(json.load(f))
            except Exception:
                pass

        for link_id, raw in self.DEFAULT_LEGACY_COORDS.items():
            if link_id not in coords_source:
                coords_source[link_id] = {
                    "lon_start": raw[0],
                    "lat_start": raw[1],
                    "lon_end": raw[2],
                    "lat_end": raw[3],
                }

        return coords_source

    def _resolve_legacy_coords(self, item: dict, coords_source: dict) -> dict:
        coords = item.get("coords", {})
        if coords:
            return coords

        resolved = coords_source.get(item["id"], {})
        if isinstance(resolved, list) and len(resolved) >= 4:
            return {
                "lon_start": resolved[0],
                "lat_start": resolved[1],
                "lon_end": resolved[2],
                "lat_end": resolved[3],
            }
        return resolved

    def _backfill_nodes_from_links(self, network: Network) -> None:
        for link in network.links.values():
            coords = link.coords or {}
            if not coords:
                continue

            start_lon = coords.get("lon_start")
            start_lat = coords.get("lat_start")
            end_lon = coords.get("lon_end")
            end_lat = coords.get("lat_end")

            if link.start_node_id in network.nodes:
                start_node = network.nodes[link.start_node_id]
                if start_node.lon is None and start_lon is not None:
                    start_node.lon = start_lon
                    start_node.lat = start_lat
                    start_node.name = start_node.name or start_node.id
            elif start_lon is not None and start_lat is not None:
                network.add_node(Node(id=link.start_node_id, lon=start_lon, lat=start_lat, name=link.start_node_id))

            if link.end_node_id in network.nodes:
                end_node = network.nodes[link.end_node_id]
                if end_node.lon is None and end_lon is not None:
                    end_node.lon = end_lon
                    end_node.lat = end_lat
                    end_node.name = end_node.name or end_node.id
            elif end_lon is not None and end_lat is not None:
                network.add_node(Node(id=link.end_node_id, lon=end_lon, lat=end_lat, name=link.end_node_id))

    def _assign_default_node_names(self, network: Network) -> None:
        ordered_nodes = sorted(network.nodes.values(), key=lambda node: node.id)
        for index, node in enumerate(ordered_nodes, start=1):
            if not node.name or self._looks_like_coordinate_node_id(node.name) or self._looks_like_coordinate_node_id(node.id):
                node.name = f"N{index}"

    def _looks_like_coordinate_node_id(self, value: str) -> bool:
        return value.startswith("N_") and "_" in value[2:]
