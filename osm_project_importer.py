from __future__ import annotations

import argparse
import math
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from models import Link, Network, Node, Project


ALLOWED_HIGHWAYS = {"primary", "secondary", "tertiary", "trunk", "residential"}
ANGLE_TOLERANCE_DEG = 3.0
SAME_ROAD_CONTINUATION_ANGLE_DEG = 25.0
DISTANCE_TOLERANCE_M = 1.5
EARTH_RADIUS_M = 6371008.8
ONEWAY_TRUE_VALUES = {"yes", "true", "1"}
ONEWAY_FALSE_VALUES = {"no", "false", "0"}


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

    graph = ox.graph_from_point(location_point, dist=dist_m, network_type="drive", simplify=True)
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
        if not highway or highway not in ALLOWED_HIGHWAYS:
            continue
        refs = [nd.get("ref") for nd in way.findall("nd") if nd.get("ref") in osm_nodes]
        if len(refs) > 1:
            highway_ways.append({"refs": refs, "tags": tags, "osm_id": way.get("id")})

    anchor_refs = _xml_anchor_refs(highway_ways, osm_nodes)
    network = Network()
    link_index = 1
    direction_stats = {"forward": 0, "reverse": 0, "bidirectional_ways": 0, "oneway_ways": 0}

    for way in highway_ways:
        tags = way["tags"]
        way_direction_info = _osm_way_direction_info(tags)
        if way_direction_info["is_bidirectional"]:
            direction_stats["bidirectional_ways"] += 1
        else:
            direction_stats["oneway_ways"] += 1

        for refs in _split_refs_at_anchors(way["refs"], anchor_refs):
            points = [osm_nodes[ref] for ref in refs]
            if len(points) < 2 or _is_zero_length_polyline(points):
                continue

            start_ref = refs[0]
            end_ref = refs[-1]
            start_node_id = _node_id(start_ref)
            end_node_id = _node_id(end_ref)
            _add_osm_node(network, start_node_id, start_ref, points[0])
            _add_osm_node(network, end_node_id, end_ref, points[-1])

            link_index = _add_directional_segment_links(
                network=network,
                link_index=link_index,
                start_node_id=start_node_id,
                end_node_id=end_node_id,
                points=points,
                tags=tags,
                default_intensity=default_intensity,
                base_metadata={
                    "source": "osm_xml",
                    "osm_way_id": way["osm_id"],
                    "osm_start_ref": start_ref,
                    "osm_end_ref": end_ref,
                    "osm_oneway_raw": tags.get("oneway", ""),
                },
                direction_stats=direction_stats,
            )

    _merge_degree_two_continuations(network)
    _classify_network_nodes(network)
    return Project(
        project_name=f"OSM File Import: {Path(path).name}",
        pcu_coefficients={"car": 1.0, "truck": 2.5, "bus": 2.0},
        network=network,
        metadata={
            "source": "osm_xml",
            "path": str(path),
            "direction_policy": {
                "rule": "OSM ways without oneway=yes are imported as two directed links.",
                "oneway_true_values": sorted(ONEWAY_TRUE_VALUES),
                "oneway_false_values": sorted(ONEWAY_FALSE_VALUES),
                "lanes_assumption": "If a bidirectional way has only total lanes, lanes are split approximately equally by direction.",
            },
            "direction_stats": direction_stats,
        },
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

        link_index = _add_segment_link(
            network,
            link_index,
            start_node_id,
            end_node_id,
            points,
            data,
            default_intensity,
            {
                "source": "osm",
                "osm_u": u,
                "osm_v": v,
                "osm_key": key,
                "oneway": data.get("oneway", False),
                "osm_direction": "graph_edge",
                "osm_is_oneway": bool(data.get("oneway", False)),
            },
            lanes_override=_parse_lanes(data.get("lanes")),
        )

    _classify_network_nodes(network)
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


def _osm_way_direction_info(tags: dict[str, Any]) -> dict[str, Any]:
    raw_oneway = _text_value(tags.get("oneway"), "").strip().lower()
    if raw_oneway in ONEWAY_TRUE_VALUES:
        directions = ["forward"]
    elif raw_oneway == "-1":
        directions = ["reverse"]
    else:
        # In OSM, a driveable way is normally bidirectional unless oneway says otherwise.
        directions = ["forward", "reverse"]

    return {
        "raw_oneway": raw_oneway,
        "directions": directions,
        "is_bidirectional": len(directions) == 2,
    }


def _add_directional_segment_links(
    network: Network,
    link_index: int,
    start_node_id: str,
    end_node_id: str,
    points: list[tuple[float, float]],
    tags: dict[str, Any],
    default_intensity: int,
    base_metadata: dict[str, Any],
    direction_stats: dict[str, int] | None = None,
) -> int:
    direction_info = _osm_way_direction_info(tags)
    is_bidirectional = direction_info["is_bidirectional"]

    for direction in direction_info["directions"]:
        if direction == "forward":
            directed_start_node_id = start_node_id
            directed_end_node_id = end_node_id
            directed_points = points
        else:
            directed_start_node_id = end_node_id
            directed_end_node_id = start_node_id
            directed_points = list(reversed(points))

        lanes = _directional_lanes(tags, direction, is_bidirectional)
        metadata = {
            **base_metadata,
            "osm_direction": direction,
            "osm_is_oneway": not is_bidirectional,
            "osm_bidirectional_source_way": is_bidirectional,
        }
        if is_bidirectional and "lanes:forward" not in tags and "lanes:backward" not in tags:
            metadata["lanes_direction_assumption"] = "split_total_lanes_equally"

        link_index = _add_segment_link(
            network,
            link_index,
            directed_start_node_id,
            directed_end_node_id,
            directed_points,
            tags,
            default_intensity,
            metadata,
            lanes_override=lanes,
        )
        if direction_stats is not None:
            direction_stats[direction] = direction_stats.get(direction, 0) + 1

    return link_index


def _directional_lanes(tags: dict[str, Any], direction: str, is_bidirectional: bool) -> int:
    if direction == "forward":
        directional = _parse_lanes(tags.get("lanes:forward"))
    else:
        directional = _parse_lanes(tags.get("lanes:backward"))

    if directional > 1 or (direction == "forward" and tags.get("lanes:forward")) or (direction == "reverse" and tags.get("lanes:backward")):
        return max(directional, 1)

    total_lanes = _parse_lanes(tags.get("lanes"))
    if is_bidirectional:
        return max(int(round(total_lanes / 2.0)), 1)
    return max(total_lanes, 1)


def _xml_anchor_refs(
    highway_ways: list[dict[str, Any]],
    osm_nodes: dict[str, tuple[float, float]],
) -> set[str]:
    ref_counts: dict[str, int] = {}
    anchor_refs: set[str] = set()
    for way in highway_ways:
        refs = way["refs"]
        if not refs:
            continue
        anchor_refs.add(refs[0])
        anchor_refs.add(refs[-1])
        for ref in refs:
            ref_counts[ref] = ref_counts.get(ref, 0) + 1

    for ref, count in ref_counts.items():
        if count > 1:
            anchor_refs.add(ref)

    return anchor_refs


def _split_refs_at_anchors(refs: list[str], anchor_refs: set[str]) -> list[list[str]]:
    if len(refs) < 2:
        return []

    chunks = []
    start_index = 0
    for index in range(1, len(refs)):
        if index == len(refs) - 1 or refs[index] in anchor_refs:
            chunk = refs[start_index : index + 1]
            if len(chunk) >= 2:
                chunks.append(chunk)
            start_index = index
    return chunks


def _is_zero_length_polyline(points: list[tuple[float, float]]) -> bool:
    return all(point == points[0] for point in points[1:])


def _add_segment_link(
    network: Network,
    link_index: int,
    start_node_id: str,
    end_node_id: str,
    points: list[tuple[float, float]],
    tags: dict[str, Any],
    default_intensity: int,
    metadata: dict[str, Any],
    lanes_override: int | None = None,
) -> int:
    lanes = lanes_override or _parse_lanes(tags.get("lanes"))
    length_km = _polyline_length_km(points)
    geometry_points = _simplify_hidden_geometry_points(points)
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
                "type": "polyline",
                "points": [[round(lon, 6), round(lat, 6)] for lon, lat in geometry_points],
                "lon_start": round(geometry_points[0][0], 6),
                "lat_start": round(geometry_points[0][1], 6),
                "lon_end": round(geometry_points[-1][0], 6),
                "lat_end": round(geometry_points[-1][1], 6),
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
                "osm_name": _text_value(tags.get("name"), ""),
                "maxspeed": _text_value(tags.get("maxspeed"), ""),
            },
        )
    )
    return link_index + 1


def _dedupe_consecutive_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    deduped = []
    for point in points:
        if not deduped or point != deduped[-1]:
            deduped.append(point)
    return deduped


def _classify_network_nodes(network: Network) -> None:
    for node_id, node in network.nodes.items():
        incoming = network.get_incoming_links(node_id)
        outgoing = network.get_outgoing_links(node_id)
        incident_links = [
            link
            for link in network.links.values()
            if link.start_node_id == node_id or link.end_node_id == node_id
        ]
        neighbors = _node_neighbor_ids(node_id, incident_links)

        if len(neighbors) <= 1 or not incoming or not outgoing:
            node.node_type = "boundary"
        elif _is_simple_continuation_topology(node_id, incoming, outgoing, incident_links):
            node.node_type = "attribute_change"
        else:
            node.node_type = "intersection"


def _node_neighbor_ids(node_id: str, incident_links: list[Link]) -> set[str]:
    neighbors: set[str] = set()
    for link in incident_links:
        if link.start_node_id == node_id and link.end_node_id != node_id:
            neighbors.add(link.end_node_id)
        if link.end_node_id == node_id and link.start_node_id != node_id:
            neighbors.add(link.start_node_id)
    return neighbors


def _merge_degree_two_continuations(network: Network) -> None:
    changed = True
    while changed:
        changed = False
        for node_id in list(network.nodes):
            incoming = [
                link
                for link in network.links.values()
                if link.end_node_id == node_id and link.start_node_id != link.end_node_id
            ]
            outgoing = [
                link
                for link in network.links.values()
                if link.start_node_id == node_id and link.start_node_id != link.end_node_id
            ]
            incident_links = [
                link
                for link in network.links.values()
                if link.start_node_id == node_id or link.end_node_id == node_id
            ]

            if not _is_simple_continuation_topology(node_id, incoming, outgoing, incident_links):
                continue

            pairs = _continuation_pairs(node_id, incoming, outgoing)
            if not pairs:
                continue

            for first_link, second_link in pairs:
                merged_link = _merged_link_through_node(first_link, second_link, node_id)
                network.links.pop(first_link.id, None)
                network.links.pop(second_link.id, None)
                network.links[merged_link.id] = merged_link
            network.nodes.pop(node_id, None)
            changed = True
            break


def _is_simple_continuation_topology(
    node_id: str,
    incoming: list[Link],
    outgoing: list[Link],
    incident_links: list[Link],
) -> bool:
    if not incoming or not outgoing or len(incoming) != len(outgoing):
        return False
    if len({link.id for link in incident_links}) != len(incident_links):
        return False

    neighbors = _node_neighbor_ids(node_id, incident_links)
    return len(neighbors) == 2


def _continuation_pairs(
    node_id: str,
    incoming: list[Link],
    outgoing: list[Link],
) -> list[tuple[Link, Link]]:
    pairs: list[tuple[Link, Link]] = []
    unused_outgoing = {link.id: link for link in outgoing}

    for in_link in sorted(incoming, key=lambda link: link.id):
        candidates = [
            out_link
            for out_link in unused_outgoing.values()
            if out_link.end_node_id != in_link.start_node_id
            and _links_can_merge(in_link, out_link)
            and _is_same_road_continuation(in_link, out_link, node_id)
        ]
        if len(candidates) != 1:
            return []

        out_link = candidates[0]
        pairs.append((in_link, out_link))
        del unused_outgoing[out_link.id]

    if unused_outgoing:
        return []
    return pairs


def _links_can_merge(first_link: Link, second_link: Link) -> bool:
    if first_link.metadata.get("disabled") or second_link.metadata.get("disabled"):
        return False

    comparable_metadata_keys = (
        "source",
        "osm_direction",
        "osm_is_oneway",
        "highway",
        "maxspeed",
    )
    for key in comparable_metadata_keys:
        if first_link.metadata.get(key) != second_link.metadata.get(key):
            return False

    if first_link.parameters.get("lanes_total", 1) != second_link.parameters.get("lanes_total", 1):
        return False
    if not _same_road_identity(first_link, second_link):
        return False

    return first_link.traffic_counts == second_link.traffic_counts


def _same_road_identity(first_link: Link, second_link: Link) -> bool:
    first_way_id = first_link.metadata.get("osm_way_id")
    if first_way_id and first_way_id == second_link.metadata.get("osm_way_id"):
        return True
    if first_link.metadata.get("osm_name") and first_link.metadata.get("osm_name") == second_link.metadata.get("osm_name"):
        return True
    return first_link.name == second_link.name


def _is_same_road_continuation(first_link: Link, second_link: Link, node_id: str) -> bool:
    _, _, points, join_index = _merged_points_through_node(first_link, second_link, node_id)
    if join_index <= 0 or join_index >= len(points) - 1:
        return False

    angle = _turn_angle_deg(points[join_index - 1], points[join_index], points[join_index + 1])
    return abs(180.0 - angle) <= SAME_ROAD_CONTINUATION_ANGLE_DEG


def _merged_link_through_node(first_link: Link, second_link: Link, node_id: str) -> Link:
    start_node_id, end_node_id, points, _ = _merged_points_through_node(first_link, second_link, node_id)
    length_km = _polyline_length_km(points)
    geometry_points = _simplify_hidden_geometry_points(points)
    name = first_link.name if first_link.name == second_link.name else f"{first_link.name} / {second_link.name}"
    coords = {
        "type": "polyline",
        "points": [[round(lon, 6), round(lat, 6)] for lon, lat in geometry_points],
        "lon_start": round(geometry_points[0][0], 6),
        "lat_start": round(geometry_points[0][1], 6),
        "lon_end": round(geometry_points[-1][0], 6),
        "lat_end": round(geometry_points[-1][1], 6),
    }
    metadata = {
        **first_link.metadata,
        "merged_link_ids": _merged_link_ids(first_link) + _merged_link_ids(second_link),
    }

    return Link(
        id=first_link.id,
        name=name,
        start_node_id=start_node_id,
        end_node_id=end_node_id,
        link_type=first_link.link_type,
        length_km=round(length_km, 4),
        traffic_counts=dict(first_link.traffic_counts),
        coords=coords,
        parameters={**first_link.parameters, "length_km": round(length_km, 4)},
        results=dict(first_link.results),
        metadata=metadata,
    )


def _merged_points_through_node(
    first_link: Link,
    second_link: Link,
    node_id: str,
) -> tuple[str, str, list[tuple[float, float]], int]:
    first_points = _link_lon_lat_points(first_link)
    second_points = _link_lon_lat_points(second_link)

    if first_link.end_node_id == node_id and second_link.start_node_id == node_id:
        start_node_id = first_link.start_node_id
        end_node_id = second_link.end_node_id
        points = first_points + second_points[1:]
        join_index = len(first_points) - 1
    elif first_link.start_node_id == node_id and second_link.end_node_id == node_id:
        start_node_id = second_link.start_node_id
        end_node_id = first_link.end_node_id
        points = second_points + first_points[1:]
        join_index = len(second_points) - 1
    elif first_link.start_node_id == node_id and second_link.start_node_id == node_id:
        start_node_id = first_link.end_node_id
        end_node_id = second_link.end_node_id
        reversed_first_points = list(reversed(first_points))
        points = reversed_first_points + second_points[1:]
        join_index = len(reversed_first_points) - 1
    else:
        start_node_id = first_link.start_node_id
        end_node_id = second_link.start_node_id
        reversed_second_points = list(reversed(second_points))
        points = first_points + reversed_second_points[1:]
        join_index = len(first_points) - 1

    points = _dedupe_consecutive_points([(float(lon), float(lat)) for lon, lat in points])
    join_index = max(1, min(join_index, len(points) - 2))
    return start_node_id, end_node_id, points, join_index


def _link_lon_lat_points(link: Link) -> list[tuple[float, float]]:
    coords = link.coords or {}
    if coords.get("type") == "polyline" and len(coords.get("points", [])) >= 2:
        return [(float(lon), float(lat)) for lon, lat in coords["points"]]

    return [
        (float(coords["lon_start"]), float(coords["lat_start"])),
        (float(coords["lon_end"]), float(coords["lat_end"])),
    ]


def _merged_link_ids(link: Link) -> list[str]:
    return list(link.metadata.get("merged_link_ids") or [link.id])


def _simplify_hidden_geometry_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    simplified = _dedupe_consecutive_points(points)
    if len(simplified) <= 2:
        return simplified

    changed = True
    while changed and len(simplified) > 2:
        changed = False
        next_points = [simplified[0]]
        for previous_point, current_point, next_point in zip(simplified, simplified[1:], simplified[2:]):
            if _is_redundant_geometry_point(previous_point, current_point, next_point):
                changed = True
                continue
            next_points.append(current_point)
        next_points.append(simplified[-1])
        simplified = next_points
    return simplified


def _is_redundant_geometry_point(
    previous_point: tuple[float, float],
    current_point: tuple[float, float],
    next_point: tuple[float, float],
) -> bool:
    if previous_point == current_point or current_point == next_point:
        return True

    angle = _turn_angle_deg(previous_point, current_point, next_point)
    if abs(180.0 - angle) > ANGLE_TOLERANCE_DEG:
        return False

    return _point_to_segment_distance_m(current_point, previous_point, next_point) <= DISTANCE_TOLERANCE_M


def _turn_angle_deg(
    previous_point: tuple[float, float],
    current_point: tuple[float, float],
    next_point: tuple[float, float],
) -> float:
    vector_a = _meter_vector(current_point, previous_point)
    vector_b = _meter_vector(current_point, next_point)
    length_a = math.hypot(vector_a[0], vector_a[1])
    length_b = math.hypot(vector_b[0], vector_b[1])
    if length_a == 0.0 or length_b == 0.0:
        return 180.0

    cos_angle = (vector_a[0] * vector_b[0] + vector_a[1] * vector_b[1]) / (length_a * length_b)
    cos_angle = max(-1.0, min(1.0, cos_angle))
    return math.degrees(math.acos(cos_angle))


def _point_to_segment_distance_m(
    point: tuple[float, float],
    segment_start: tuple[float, float],
    segment_end: tuple[float, float],
) -> float:
    point_xy = _local_xy_m(point, point[1])
    start_xy = _local_xy_m(segment_start, point[1])
    end_xy = _local_xy_m(segment_end, point[1])

    segment_x = end_xy[0] - start_xy[0]
    segment_y = end_xy[1] - start_xy[1]
    segment_length_sq = segment_x * segment_x + segment_y * segment_y
    if segment_length_sq == 0.0:
        return math.hypot(point_xy[0] - start_xy[0], point_xy[1] - start_xy[1])

    point_x = point_xy[0] - start_xy[0]
    point_y = point_xy[1] - start_xy[1]
    projection = (point_x * segment_x + point_y * segment_y) / segment_length_sq
    projection = max(0.0, min(1.0, projection))
    closest_x = start_xy[0] + projection * segment_x
    closest_y = start_xy[1] + projection * segment_y
    return math.hypot(point_xy[0] - closest_x, point_xy[1] - closest_y)


def _meter_vector(origin: tuple[float, float], target: tuple[float, float]) -> tuple[float, float]:
    origin_xy = _local_xy_m(origin, origin[1])
    target_xy = _local_xy_m(target, origin[1])
    return target_xy[0] - origin_xy[0], target_xy[1] - origin_xy[1]


def _local_xy_m(point: tuple[float, float], origin_lat: float) -> tuple[float, float]:
    lon, lat = point
    return (
        math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(origin_lat)),
        math.radians(lat) * EARTH_RADIUS_M,
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import OSM XML into osm_network_project_map_nstu.json.")
    parser.add_argument("--input", default="map_nstu.osm", help="Input OSM XML file.")
    parser.add_argument("--output", default="osm_network_project_map_nstu.json", help="Output project JSON file.")
    parser.add_argument("--intensity", type=int, default=600, help="Default car intensity per link.")
    args = parser.parse_args(argv)

    from project_saver import ProjectSaver

    project = build_project_from_osm_xml(args.input, args.intensity)
    ProjectSaver().save(project, args.output)
    print(
        f"Saved {args.output}: "
        f"{len(project.network.nodes)} nodes, {len(project.network.links)} links."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
