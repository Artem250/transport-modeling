from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import re
from pathlib import Path
from typing import Any


DEFAULT_INPUT = "nsk_roads_bbox_3_segments_2.csv"
DEFAULT_OUTPUT = "skdf_segments_project.json"


def write_project_from_segments_csv(
    csv_path: str | Path = DEFAULT_INPUT,
    output_path: str | Path = DEFAULT_OUTPUT,
) -> dict[str, int]:
    project = build_project_from_segments_csv(csv_path)
    output_path = Path(output_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(project, f, indent=2, ensure_ascii=False)
    return {
        "nodes": len(project["network"]["nodes"]),
        "links": len(project["network"]["links"]),
    }


def build_project_from_segments_csv(csv_path: str | Path) -> dict[str, Any]:
    rows = _read_rows(csv_path)
    nodes: dict[tuple[float, float], dict[str, Any]] = {}
    links: list[dict[str, Any]] = []

    for row_index, row in enumerate(rows, start=1):
        geometry = _parse_multiline(row.get("geometry_segment") or row.get("geometry"))
        if not geometry:
            continue

        intensity = _number(row.get("traffic_segment"), row.get("traffic_1"), row.get("traffic"))
        capacity = _number(row.get("capacity_segment"), row.get("capacity_1"), row.get("capacity"))
        lanes = _number(row.get("lanes_segment"), row.get("lanes_1"), row.get("lanes"))
        speed = _number(row.get("top_speed_segment"), row.get("speed_limit_1"), row.get("speed_limit"))
        road_name = _text(row.get("road_name_segment") or row.get("road_name") or row.get("full_name"))
        segment_id = _text(row.get("segment_object_id") or row.get("segment_feature_id") or row_index)
        start_km = _number(row.get("start_km_segment"))
        finish_km = _number(row.get("finish_km_segment"))

        for part_index, raw_line in enumerate(geometry, start=1):
            lonlat_points = _to_lonlat_points(raw_line)
            if len(lonlat_points) < 2:
                continue

            start_node_id = _node_id(nodes, lonlat_points[0])
            end_node_id = _node_id(nodes, lonlat_points[-1])
            link_id = _link_id(segment_id, part_index, len(geometry))
            vc_ratio = _vc_ratio(intensity, capacity)
            name = _segment_name(road_name, start_km, finish_km, part_index, len(geometry))
            length_km = _line_length_km(lonlat_points)

            links.append(
                {
                    "id": link_id,
                    "name": name,
                    "start_node_id": start_node_id,
                    "end_node_id": end_node_id,
                    "link_type": "skdf_segment",
                    "length_km": round(length_km, 4),
                    "traffic_counts": {"car": intensity} if intensity is not None else {},
                    "observed_counts": {"car": intensity} if intensity is not None else {},
                    "coords": {
                        "type": "polyline",
                        "points": [[round(lon, 7), round(lat, 7)] for lon, lat in lonlat_points],
                        "lon_start": round(lonlat_points[0][0], 7),
                        "lat_start": round(lonlat_points[0][1], 7),
                        "lon_end": round(lonlat_points[-1][0], 7),
                        "lat_end": round(lonlat_points[-1][1], 7),
                    },
                    "parameters": _parameters(row, capacity, lanes, speed, start_km, finish_km),
                    "results": _results(link_id, name, intensity, capacity, vc_ratio, length_km),
                    "metadata": _metadata(row, intensity, capacity, lanes, speed, part_index, len(geometry)),
                }
            )

    return {
        "project_name": f"SKDF segment traffic map: {Path(csv_path).name}",
        "pcu_coefficients": {"car": 1.0, "truck": 2.5, "bus": 2.0},
        "analysis_mode": "dynamic",
        "metadata": {
            "source": "skdf_segments_csv",
            "csv_path": str(csv_path),
            "links_total": len(links),
        },
        "network": {
            "nodes": list(nodes.values()),
            "links": links,
            "routes": [],
            "sources": [],
            "sinks": [],
            "movements": [],
        },
        "scenarios": [],
    }


def _read_rows(csv_path: str | Path) -> list[dict[str, str]]:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = _detect_csv_delimiter(sample)
        return list(csv.DictReader(f, delimiter=delimiter))


def _detect_csv_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        return dialect.delimiter
    except csv.Error:
        return ";" if sample.count(";") >= sample.count(",") else ","


def _parse_multiline(raw: str | None) -> list[list[list[float]]]:
    if not raw:
        return []
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return []
    if not isinstance(value, list) or not value:
        return []

    if _looks_like_point(value[0]):
        value = [value]

    lines: list[list[list[float]]] = []
    for raw_line in value:
        line = []
        for point in raw_line:
            if _looks_like_point(point):
                line.append([float(point[0]), float(point[1])])
        if len(line) >= 2:
            lines.append(line)
    return lines


def _looks_like_point(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) >= 2 and not isinstance(value[0], (list, tuple))


def _to_lonlat_points(raw_line: list[list[float]]) -> list[tuple[float, float]]:
    transformer = _transformer_3857_to_4326()
    points = []
    for x, y in raw_line:
        lon, lat = transformer.transform(x, y)
        points.append((lon, lat))
    return points


def _transformer_3857_to_4326():
    from pyproj import Transformer

    if not hasattr(_transformer_3857_to_4326, "_transformer"):
        _transformer_3857_to_4326._transformer = Transformer.from_crs(
            "EPSG:3857",
            "EPSG:4326",
            always_xy=True,
        )
    return _transformer_3857_to_4326._transformer


def _node_id(nodes: dict[tuple[float, float], dict[str, Any]], point: tuple[float, float]) -> str:
    lon, lat = point
    key = (round(lon, 7), round(lat, 7))
    if key not in nodes:
        node_id = f"SKDF_N{len(nodes) + 1}"
        nodes[key] = {
            "id": node_id,
            "lon": key[0],
            "lat": key[1],
            "x": None,
            "y": None,
            "node_type": "ordinary",
            "name": node_id,
            "metadata": {"source": "skdf_segment_endpoint"},
        }
    return nodes[key]["id"]


def _link_id(segment_id: str, part_index: int, part_count: int) -> str:
    safe_segment_id = re.sub(r"[^0-9A-Za-z_-]+", "_", segment_id).strip("_") or "row"
    base = f"SKDF_{safe_segment_id}"
    if part_count > 1:
        return f"{base}_P{part_index}"
    return base


def _segment_name(road_name: str, start_km: float | None, finish_km: float | None, part_index: int, part_count: int) -> str:
    name = road_name or "SKDF segment"
    if start_km is not None and finish_km is not None:
        name = f"{name} {start_km:g}-{finish_km:g} km"
    if part_count > 1:
        name = f"{name} part {part_index}"
    return name


def _parameters(
    row: dict[str, str],
    capacity: float | None,
    lanes: float | None,
    speed: float | None,
    start_km: float | None,
    finish_km: float | None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "start_km_skdf": start_km,
        "finish_km_skdf": finish_km,
        "capacity_total_skdf": capacity,
        "lanes_total": int(round(lanes)) if lanes is not None else None,
        "speed_limit_skdf": speed,
        "roadway_width_skdf": _number(row.get("roadway_width_segment")),
        "roadbed_width_skdf": _number(row.get("roadbed_width_segment")),
    }
    return {key: value for key, value in parameters.items() if value is not None}


def _results(
    link_id: str,
    name: str,
    intensity: float | None,
    capacity: float | None,
    vc_ratio: float | None,
    length_km: float,
) -> dict[str, Any]:
    return {
        "id": link_id,
        "name": name,
        "type": "SKDFSegment",
        "V": intensity,
        "C_initial": capacity,
        "VC_ratio": vc_ratio,
        "LOS": _los(vc_ratio),
        "Delay_sec": 0,
        "Length_km": round(length_km, 4),
    }


def _metadata(
    row: dict[str, str],
    intensity: float | None,
    capacity: float | None,
    lanes: float | None,
    speed: float | None,
    part_index: int,
    part_count: int,
) -> dict[str, Any]:
    return {
        "source": "skdf_segment",
        "skdf": {
            "source": "skdf_segment",
            "road_id": _text(row.get("road_id")),
            "road_part_id": _text(row.get("road_part_id_segment") or row.get("road_part_id")),
            "segment_object_id": _text(row.get("segment_object_id")),
            "segment_feature_id": _text(row.get("segment_feature_id")),
            "road_name": _text(row.get("road_name_segment") or row.get("road_name")),
            "full_name": _text(row.get("full_name")),
            "start_km": _number(row.get("start_km_segment")),
            "finish_km": _number(row.get("finish_km_segment")),
            "traffic": intensity,
            "capacity_total": capacity,
            "lanes": int(round(lanes)) if lanes is not None else None,
            "speed_limit": speed,
            "directional": False,
            "part_index": part_index,
            "part_count": part_count,
        },
    }


def _vc_ratio(intensity: float | None, capacity: float | None) -> float | None:
    if intensity is None or capacity is None or capacity <= 0:
        return None
    return round(intensity / capacity, 3)


def _los(vc_ratio: float | None) -> str:
    if vc_ratio is None:
        return "UNDEFINED"
    if vc_ratio <= 0.35:
        return "A"
    if vc_ratio <= 0.54:
        return "B"
    if vc_ratio <= 0.77:
        return "C"
    if vc_ratio <= 0.93:
        return "D"
    if vc_ratio <= 1.0:
        return "E"
    return "F"


def _line_length_km(points: list[tuple[float, float]]) -> float:
    try:
        from pyproj import Geod

        if not hasattr(_line_length_km, "_geod"):
            _line_length_km._geod = Geod(ellps="WGS84")
        total = 0.0
        for start, end in zip(points, points[1:]):
            total += abs(_line_length_km._geod.inv(start[0], start[1], end[0], end[1])[2])
        return total / 1000
    except Exception:
        return sum(_haversine_km(start, end) for start, end in zip(points, points[1:]))


def _haversine_km(start: tuple[float, float], end: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, start)
    lon2, lat2 = map(math.radians, end)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _number(*values: Any) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isnan(value):
                continue
            return float(value)
        text = str(value).strip()
        if not text:
            continue
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                parsed_value = _number(*parsed)
                if parsed_value is not None:
                    return parsed_value
            continue
        try:
            return float(text.replace(",", "."))
        except ValueError:
            continue
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a traffic_viz project JSON from SKDF segment CSV.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="CSV from api_test.py with geometry_segment fields.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output project JSON for traffic_viz.py.")
    args = parser.parse_args()

    stats = write_project_from_segments_csv(args.input, args.output)
    print(f"Saved {Path(args.output).resolve()}: {stats['links']} links, {stats['nodes']} nodes")


if __name__ == "__main__":
    main()
