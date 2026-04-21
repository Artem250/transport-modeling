from __future__ import annotations

import math
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from models import Link, Network, Node, Project


class OsmImportError(RuntimeError):
    pass


def build_project_from_osm_point(
    location_point: tuple[float, float],
    dist_m: int = 1500,
    default_intensity: int = 600,
) -> Project:
    try:
        import osmnx as ox
    except ImportError as exc:
        raise OsmImportError(
            "Для загрузки участка карты нужен пакет osmnx.\n"
            f"Текущий Python: {sys.executable}\n"
            f"Команда установки: \"{sys.executable}\" -m pip install osmnx"
        ) from exc

    graph = ox.graph_from_point(location_point, dist=dist_m, network_type="drive", simplify=False)
    return build_project_from_osmnx_graph(graph, default_intensity)


def build_project_from_osm_xml(path: str | Path, default_intensity: int = 600) -> Project:
    tree = ET.parse(path)
    root = tree.getroot()

    osm_nodes = {
        node.get("id"): (float(node.get("lon")), float(node.get("lat")))
        for node in root.findall(".//node")
        if node.get("id") and node.get("lon") and node.get("lat")
    }

    highway_ways = []
    for way in root.findall(".//way"):
        tags = {tag.get("k"): tag.get("v") for tag in way.findall("tag") if tag.get("k")}
        highway = tags.get("highway")
        if not highway or highway in {"footway", "path", "cycleway", "pedestrian", "steps"}:
            continue
        refs = [nd.get("ref") for nd in way.findall("nd") if nd.get("ref") in osm_nodes]
        if len(refs) > 1:
            highway_ways.append({"refs": refs, "tags": tags, "osm_id": way.get("id")})

    network = Network()
    link_index = 1

    for way in highway_ways:
        refs = way["refs"]
        for start_ref, end_ref in zip(refs, refs[1:]):
            start_point = osm_nodes[start_ref]
            end_point = osm_nodes[end_ref]
            if start_point == end_point:
                continue

            start_node_id = _node_id(start_ref)
            end_node_id = _node_id(end_ref)
            _add_osm_node(network, start_node_id, start_ref, start_point)
            _add_osm_node(network, end_node_id, end_ref, end_point)

            tags = way["tags"]
            link_index = _add_segment_link(
                network,
                link_index,
                start_node_id,
                end_node_id,
                [start_point, end_point],
                tags,
                default_intensity,
                {
                    "source": "osm_xml",
                    "osm_way_id": way["osm_id"],
                    "osm_start_ref": start_ref,
                    "osm_end_ref": end_ref,
                },
            )

    return Project(
        project_name=f"OSM File Import: {Path(path).name}",
        pcu_coefficients={"car": 1.0, "truck": 2.5, "bus": 2.0},
        network=network,
        metadata={"source": "osm_xml", "path": str(path)},
    )


def build_project_from_osmnx_graph(graph: Any, default_intensity: int = 600) -> Project:
    network = Network()

    for osm_id, data in graph.nodes(data=True):
        lon = data.get("x")
        lat = data.get("y")
        if lon is None or lat is None:
            continue
        node_id = _node_id(osm_id)
        network.add_node(
            Node(
                id=node_id,
                lon=round(float(lon), 6),
                lat=round(float(lat), 6),
                name=node_id,
                metadata={"source": "osm", "osm_id": osm_id},
            )
        )

    link_index = 1
    for u, v, key, data in graph.edges(keys=True, data=True):
        start_node_id = _node_id(u)
        end_node_id = _node_id(v)
        if start_node_id not in network.nodes or end_node_id not in network.nodes:
            continue

        points = _edge_points(data, network.nodes[start_node_id], network.nodes[end_node_id])
        if len(points) < 2:
            continue

        segment_node_ids = [start_node_id]
        for point_index, point in enumerate(points[1:-1], start=1):
            segment_node_id = f"OSM_GEOM_{u}_{v}_{key}_{point_index}"
            _add_osm_node(network, segment_node_id, segment_node_id, point)
            segment_node_ids.append(segment_node_id)
        segment_node_ids.append(end_node_id)

        for segment_start_id, segment_end_id, point_a, point_b in zip(
            segment_node_ids,
            segment_node_ids[1:],
            points,
            points[1:],
        ):
            link_index = _add_segment_link(
                network,
                link_index,
                segment_start_id,
                segment_end_id,
                [point_a, point_b],
                data,
                default_intensity,
                {
                    "source": "osm",
                    "osm_u": u,
                    "osm_v": v,
                    "osm_key": key,
                    "oneway": data.get("oneway", False),
                },
            )

    return Project(
        project_name="OSM Imported Network",
        pcu_coefficients={"car": 1.0, "truck": 2.5, "bus": 2.0},
        network=network,
        metadata={"source": "osmnx"},
    )


def _node_id(osm_id: Any) -> str:
    return f"OSM_{osm_id}"


def _add_osm_node(network: Network, node_id: str, osm_ref: Any, point: tuple[float, float]) -> None:
    if node_id in network.nodes:
        return
    lon, lat = point
    network.add_node(
        Node(
            id=node_id,
            lon=round(lon, 6),
            lat=round(lat, 6),
            name=node_id,
            metadata={"source": "osm", "osm_id": osm_ref},
        )
    )


def _add_segment_link(
    network: Network,
    link_index: int,
    start_node_id: str,
    end_node_id: str,
    points: list[tuple[float, float]],
    tags: dict[str, Any],
    default_intensity: int,
    metadata: dict[str, Any],
) -> int:
    lanes = _parse_lanes(tags.get("lanes"))
    length_km = _polyline_length_km(points)
    link_id = f"L{link_index}"
    network.add_link(
        Link(
            id=link_id,
            name=_text_value(tags.get("name"), f"OSM road {link_index}"),
            start_node_id=start_node_id,
            end_node_id=end_node_id,
            link_type="straight",
            length_km=round(length_km, 4),
            traffic_counts={"car": default_intensity},
            coords={
                "lon_start": round(points[0][0], 6),
                "lat_start": round(points[0][1], 6),
                "lon_end": round(points[-1][0], 6),
                "lat_end": round(points[-1][1], 6),
            },
            parameters={
                "length_km": round(length_km, 4),
                "lanes_total": lanes,
                "lanes_bus": 0,
                "capacity_per_lane_base": 1800,
                "lane_width_m": 3.5,
                "grade_percent": 0.0,
                "parking_present": False,
                "heavy_vehicles_percent": 0.0,
            },
            metadata={
                **metadata,
                "highway": _text_value(tags.get("highway"), ""),
                "maxspeed": _text_value(tags.get("maxspeed"), ""),
            },
        )
    )
    return link_index + 1


def _edge_points(data: dict[str, Any], start_node: Node, end_node: Node) -> list[tuple[float, float]]:
    geometry = data.get("geometry")
    if geometry is not None and hasattr(geometry, "coords"):
        return [(float(lon), float(lat)) for lon, lat in geometry.coords]

    if start_node.lon is None or start_node.lat is None or end_node.lon is None or end_node.lat is None:
        return []
    return [(float(start_node.lon), float(start_node.lat)), (float(end_node.lon), float(end_node.lat))]


def _parse_lanes(value: Any) -> int:
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
        for item in value:
            parsed = _parse_lanes(item)
            if parsed:
                return parsed
        return 1

    if value is None:
        return 1

    text = str(value).replace(";", "|").replace(",", "|")
    for part in text.split("|"):
        part = part.strip()
        if part.isdigit():
            return max(int(part), 1)
    return 1


def _length_km(data: dict[str, Any], points: list[tuple[float, float]]) -> float:
    raw_length = data.get("length")
    try:
        if raw_length is not None:
            return max(float(raw_length) / 1000.0, 0.001)
    except (TypeError, ValueError):
        pass
    return max(_polyline_length_km(points), 0.001)


def _polyline_length_km(points: list[tuple[float, float]]) -> float:
    total = 0.0
    for point_a, point_b in zip(points, points[1:]):
        total += _haversine_km(point_a[0], point_a[1], point_b[0], point_b[1])
    return max(total, 0.001)


def _haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    radius_km = 6371.0088
    d_lat = math.radians(lat2 - lat1)
    d_lon = math.radians(lon2 - lon1)
    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    a = math.sin(d_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(d_lon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(a))


def _text_value(value: Any, default: str) -> str:
    if isinstance(value, list):
        value = ", ".join(str(item) for item in value if item)
    if value is None or value == "":
        return default
    return str(value)
