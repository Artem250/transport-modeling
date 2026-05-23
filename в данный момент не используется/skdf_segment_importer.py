from __future__ import annotations

import argparse
import ast
import csv
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_INPUT = "nsk_roads_bbox_3_segments_2.csv"
DEFAULT_OUTPUT = "skdf_segments_project_clean.json"


@dataclass(frozen=True)
class SkdfImportConfig:
    """Configuration for importing SKDF segment CSV files.

    The importer intentionally keeps SKDF-derived values separate from model
    assumptions. It also skips composite corridor-like records by default, e.g.
    "ул. A - мост B - пр. C", because such rows do not describe one named road
    consistently enough for local network modelling.
    """

    skip_composite_roads: bool = True
    composite_separator_pattern: str = r"\s+-\s+"
    min_geometry_points: int = 2
    default_vehicle_type: str = "car"


@dataclass
class SkdfImportStats:
    rows_total: int = 0
    rows_imported: int = 0
    rows_skipped_no_geometry: int = 0
    rows_skipped_composite_road: int = 0
    links_created: int = 0
    nodes_created: int = 0
    skipped_composite_road_names: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_total": self.rows_total,
            "rows_imported": self.rows_imported,
            "rows_skipped_no_geometry": self.rows_skipped_no_geometry,
            "rows_skipped_composite_road": self.rows_skipped_composite_road,
            "links_created": self.links_created,
            "nodes_created": self.nodes_created,
            "skipped_composite_road_names": self.skipped_composite_road_names[:50],
        }


def write_project_from_segments_csv(
    csv_path: str | Path = DEFAULT_INPUT,
    output_path: str | Path = DEFAULT_OUTPUT,
    config: SkdfImportConfig | None = None,
) -> dict[str, Any]:
    project, stats = build_project_from_segments_csv(csv_path, config=config)
    output_path = Path(output_path)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(project, f, indent=2, ensure_ascii=False)
    return stats.as_dict()


def build_project_from_segments_csv(
    csv_path: str | Path,
    config: SkdfImportConfig | None = None,
) -> tuple[dict[str, Any], SkdfImportStats]:
    config = config or SkdfImportConfig()
    rows = _read_rows(csv_path)
    stats = SkdfImportStats(rows_total=len(rows))
    nodes: dict[tuple[float, float], dict[str, Any]] = {}
    links: list[dict[str, Any]] = []

    for row_index, row in enumerate(rows, start=1):
        road_name = _text(row.get("road_name_segment") or row.get("road_name") or row.get("full_name"))
        if config.skip_composite_roads and is_composite_road_name(road_name, config):
            stats.rows_skipped_composite_road += 1
            if road_name and road_name not in stats.skipped_composite_road_names:
                stats.skipped_composite_road_names.append(road_name)
            continue

        geometry = _parse_multiline(row.get("geometry_segment") or row.get("geometry"))
        if not geometry:
            stats.rows_skipped_no_geometry += 1
            continue

        intensity = _number(row.get("traffic_segment"), row.get("traffic_1"), row.get("traffic"))
        capacity = _number(row.get("capacity_segment"), row.get("capacity_1"), row.get("capacity"))
        lanes = _number(row.get("lanes_segment"), row.get("lanes_1"), row.get("lanes"))
        speed = _number(row.get("top_speed_segment"), row.get("speed_limit_1"), row.get("speed_limit"))
        roadway_width = _number(row.get("roadway_width_segment"))
        segment_id = _text(row.get("segment_object_id") or row.get("segment_feature_id") or row_index)
        road_part_id = _text(row.get("road_part_id_segment") or row.get("road_part_id"))
        start_km = _number(row.get("start_km_segment"))
        finish_km = _number(row.get("finish_km_segment"))

        row_imported = False
        for part_index, raw_line in enumerate(geometry, start=1):
            lonlat_points = _to_lonlat_points(raw_line)
            if len(lonlat_points) < config.min_geometry_points:
                continue

            start_node_id = _node_id(nodes, lonlat_points[0])
            end_node_id = _node_id(nodes, lonlat_points[-1])
            link_id = _link_id(segment_id, part_index, len(geometry))
            name = _segment_name(road_name, start_km, finish_km, part_index, len(geometry))
            length_km = _resolve_length_km(start_km, finish_km, lonlat_points)

            links.append(
                {
                    "id": link_id,
                    "name": name,
                    "start_node_id": start_node_id,
                    "end_node_id": end_node_id,
                    "link_type": "skdf_segment",
                    "length_km": round(length_km, 4),
                    "traffic_counts": ({config.default_vehicle_type: intensity} if intensity is not None else {}),
                    "coords": {
                        "type": "polyline",
                        "points": [[round(lon, 7), round(lat, 7)] for lon, lat in lonlat_points],
                        "lon_start": round(lonlat_points[0][0], 7),
                        "lat_start": round(lonlat_points[0][1], 7),
                        "lon_end": round(lonlat_points[-1][0], 7),
                        "lat_end": round(lonlat_points[-1][1], 7),
                    },
                    "parameters": _parameters(
                        capacity=capacity,
                        lanes=lanes,
                        speed=speed,
                        roadway_width=roadway_width,
                        start_km=start_km,
                        finish_km=finish_km,
                    ),
                    "results": {},
                    "metadata": _metadata(
                        row=row,
                        road_name=road_name,
                        road_part_id=road_part_id,
                        segment_id=segment_id,
                        intensity=intensity,
                        capacity=capacity,
                        lanes=lanes,
                        speed=speed,
                        roadway_width=roadway_width,
                        start_km=start_km,
                        finish_km=finish_km,
                        part_index=part_index,
                        part_count=len(geometry),
                    ),
                    "data_sources": _data_sources(),
                }
            )
            row_imported = True

        if row_imported:
            stats.rows_imported += 1

    stats.links_created = len(links)
    stats.nodes_created = len(nodes)
    project = {
        "project_name": f"Clean SKDF segment project: {Path(csv_path).name}",
        "schema_version": "2.0-draft",
        "pcu_coefficients": {"car": 1.0, "truck": 2.5, "bus": 2.0},
        "demand_model": {},
        "metadata": {
            "source": "skdf_segments_csv",
            "csv_path": str(csv_path),
            "import_policy": {
                "skip_composite_roads": config.skip_composite_roads,
                "composite_separator_pattern": config.composite_separator_pattern,
                "note": "Composite corridor-like road names are skipped because they mix multiple roads into one SKDF record.",
            },
            "skdf_directionality": {
                "scope": "unknown_probably_both_directions",
                "note": "SKDF segment fields do not explicitly separate directions. Directed OSM graph enrichment must decide how to split traffic/capacity by direction.",
            },
            "import_stats": stats.as_dict(),
        },
        "network": {
            "nodes": list(nodes.values()),
            "links": links,
            "routes": [],
        },
        "scenarios": [],
        "results": {},
    }
    return project, stats


def is_composite_road_name(name: str, config: SkdfImportConfig | None = None) -> bool:
    config = config or SkdfImportConfig()
    text = _text(name)
    if not text:
        return False
    # We intentionally look for a spaced dash. This avoids rejecting names where
    # a hyphen is part of the official road name.
    parts = [part.strip() for part in re.split(config.composite_separator_pattern, text) if part.strip()]
    return len(parts) > 1


def _parameters(
    capacity: float | None,
    lanes: float | None,
    speed: float | None,
    roadway_width: float | None,
    start_km: float | None,
    finish_km: float | None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "start_km_skdf": start_km,
        "finish_km_skdf": finish_km,
        "capacity_total_skdf": capacity,
        "lanes_total_skdf": int(round(lanes)) if lanes is not None else None,
        "speed_limit_skdf": speed,
        "roadway_width_skdf": roadway_width,
    }
    return {key: value for key, value in parameters.items() if value is not None}


def _metadata(
    row: dict[str, str],
    road_name: str,
    road_part_id: str,
    segment_id: str,
    intensity: float | None,
    capacity: float | None,
    lanes: float | None,
    speed: float | None,
    roadway_width: float | None,
    start_km: float | None,
    finish_km: float | None,
    part_index: int,
    part_count: int,
) -> dict[str, Any]:
    return {
        "source": "skdf_segment",
        "skdf": {
            "road_id": _text(row.get("road_id")),
            "road_part_id": road_part_id,
            "segment_object_id": segment_id,
            "segment_feature_id": _text(row.get("segment_feature_id")),
            "road_name": road_name,
            "full_name": _text(row.get("full_name")),
            "start_km": start_km,
            "finish_km": finish_km,
            "traffic": intensity,
            "capacity_total": capacity,
            "lanes": int(round(lanes)) if lanes is not None else None,
            "speed_limit": speed,
            "roadway_width": roadway_width,
            "part_index": part_index,
            "part_count": part_count,
            "directionality": "not_separated_in_skdf_segment_csv",
        },
    }


def _data_sources() -> dict[str, str]:
    return {
        "traffic_counts.car": "skdf.traffic_segment",
        "parameters.capacity_total_skdf": "skdf.capacity_segment",
        "parameters.lanes_total_skdf": "skdf.lanes_segment",
        "parameters.speed_limit_skdf": "skdf.top_speed_segment",
        "parameters.roadway_width_skdf": "skdf.roadway_width_segment",
        "parameters.start_km_skdf": "skdf.start_km_segment",
        "parameters.finish_km_skdf": "skdf.finish_km_segment",
        "coords": "skdf.geometry_segment",
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
    safe_segment_id = re.sub(r"[^0-9A-Za-zА-Яа-я_-]+", "_", segment_id).strip("_") or "row"
    base = f"SKDF_{safe_segment_id}"
    if part_count > 1:
        return f"{base}_P{part_index}"
    return base


def _segment_name(
    road_name: str,
    start_km: float | None,
    finish_km: float | None,
    part_index: int,
    part_count: int,
) -> str:
    name = road_name or "SKDF segment"
    if start_km is not None and finish_km is not None:
        name = f"{name} {start_km:g}-{finish_km:g} km"
    if part_count > 1:
        name = f"{name} part {part_index}"
    return name


def _resolve_length_km(
    start_km: float | None,
    finish_km: float | None,
    points: list[tuple[float, float]],
) -> float:
    if start_km is not None and finish_km is not None and finish_km > start_km:
        return finish_km - start_km
    return _line_length_km(points)


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
    parser = argparse.ArgumentParser(description="Build a clean project JSON from SKDF segment CSV.")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="CSV with SKDF segment rows.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output project JSON.")
    parser.add_argument(
        "--keep-composite-roads",
        action="store_true",
        help="Do not skip corridor-like road names containing spaced dashes.",
    )
    args = parser.parse_args()

    config = SkdfImportConfig(skip_composite_roads=not args.keep_composite_roads)
    stats = write_project_from_segments_csv(args.input, args.output, config=config)
    print(f"Saved {Path(args.output).resolve()}: {stats['links_created']} links, {stats['nodes_created']} nodes")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
