from __future__ import annotations

import ast
import csv
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models import Link, Project


@dataclass(frozen=True)
class SkdfMatchConfig:
    max_distance_m: float = 35.0
    buffer_m: float = 25.0
    min_overlap_ratio: float = 0.45
    min_score: float = 0.55
    name_bonus: float = 0.25
    name_mismatch_penalty: float = 0.20
    reject_named_mismatches: bool = True
    allow_strong_geometry_name_override: bool = False
    strong_geometry_distance_m: float = 5.0
    strong_geometry_overlap_ratio: float = 0.85
    way_group_enabled: bool = True
    way_group_max_distance_m: float = 60.0


@dataclass(frozen=True)
class SkdfRoad:
    row_index: int
    road_id: str
    road_part_id: str
    road_name: str
    full_name: str
    traffic: float | None
    capacity: float | None
    lanes: int | None
    speed_limit: float | None
    geometry: Any
    normalized_name: str


@dataclass(frozen=True)
class LinkMatch:
    link_id: str
    road: SkdfRoad
    score: float
    distance_m: float
    overlap_ratio: float
    name_similarity: float
    source: str = "direct"


@dataclass(frozen=True)
class EnrichmentStats:
    skdf_roads_loaded: int
    links_total: int
    links_with_geometry: int
    links_matched: int
    links_updated_traffic: int
    links_updated_capacity: int


def load_skdf_roads(csv_path: str | Path) -> list[SkdfRoad]:
    try:
        from shapely.geometry import MultiLineString
    except ImportError as exc:
        raise RuntimeError("Package shapely is required for SKDF matching.") from exc

    roads: list[SkdfRoad] = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = _detect_csv_delimiter(sample)
        reader = csv.DictReader(f, delimiter=delimiter)
        for row_index, row in enumerate(reader, start=1):
            geometry = _parse_skdf_geometry(row.get("geometry_segment") or row.get("geometry"), MultiLineString)
            if geometry is None or geometry.is_empty:
                continue

            road_name = _text(row.get("road_name_segment") or row.get("road_name"))
            full_name = _text(row.get("full_name"))
            name = road_name or full_name
            roads.append(
                SkdfRoad(
                    row_index=row_index,
                    road_id=_text(row.get("road_id")),
                    road_part_id=_text(row.get("road_part_id_segment") or row.get("road_part_id")),
                    road_name=road_name,
                    full_name=full_name,
                    traffic=_first_number(row, "traffic_segment", "traffic_1", "traffic"),
                    capacity=_first_number(row, "capacity_segment", "capacity_1", "capacity"),
                    lanes=_first_int_number(row, "lanes_segment", "lanes_1", "lanes"),
                    speed_limit=_first_number(row, "top_speed_segment", "speed_limit_1", "speed_limit"),
                    geometry=geometry,
                    normalized_name=normalize_road_name(name),
                )
            )
    return roads


def _detect_csv_delimiter(sample: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
        return dialect.delimiter
    except csv.Error:
        return ";" if sample.count(";") >= sample.count(",") else ","


def enrich_project_with_skdf(
    project: Project,
    csv_path: str | Path,
    config: SkdfMatchConfig | None = None,
    report_path: str | Path | None = None,
) -> EnrichmentStats:
    config = config or SkdfMatchConfig()
    roads = load_skdf_roads(csv_path)
    matcher = _SkdfMatcher(roads, config)
    links_total = len(project.network.links)
    links_with_geometry = 0
    links_matched = 0
    links_updated_traffic = 0
    links_updated_capacity = 0
    link_geometries: dict[str, Any] = {}
    matches: dict[str, LinkMatch] = {}
    best_candidates: dict[str, LinkMatch | None] = {}

    for link in project.network.links.values():
        line = _link_geometry_3857(link)
        link_geometries[link.id] = line
        if line is None or line.is_empty:
            continue

        links_with_geometry += 1
        match = matcher.match_link(link, line)
        best_candidates[link.id] = matcher.best_candidate(link.name, line, respect_acceptance=False)
        if match is None:
            continue

        matches[link.id] = match

    if config.way_group_enabled:
        _assign_way_group_matches(project, matcher, link_geometries, matches, best_candidates)

    report_rows: list[dict[str, Any]] = []
    for link in project.network.links.values():
        match = matches.get(link.id)
        best_candidate = best_candidates.get(link.id)
        line = link_geometries.get(link.id)
        if line is None or line.is_empty:
            report_rows.append(_report_row(link, None, None, "no_link_geometry"))
            continue

        if match is None:
            report_rows.append(_report_row(link, None, best_candidate, "no_match"))
            continue

        links_matched += 1
        updated_traffic, updated_capacity = _apply_match(link, match)
        links_updated_traffic += int(updated_traffic)
        links_updated_capacity += int(updated_capacity)
        report_rows.append(_report_row(link, match, best_candidate, f"matched_{match.source}"))

    stats = EnrichmentStats(
        skdf_roads_loaded=len(roads),
        links_total=links_total,
        links_with_geometry=links_with_geometry,
        links_matched=links_matched,
        links_updated_traffic=links_updated_traffic,
        links_updated_capacity=links_updated_capacity,
    )

    project.metadata = {
        **(project.metadata or {}),
        "skdf_enrichment": {
            "csv_path": str(csv_path),
            "skdf_roads_loaded": stats.skdf_roads_loaded,
            "links_total": stats.links_total,
            "links_with_geometry": stats.links_with_geometry,
            "links_matched": stats.links_matched,
            "links_updated_traffic": stats.links_updated_traffic,
            "links_updated_capacity": stats.links_updated_capacity,
            "max_distance_m": config.max_distance_m,
            "buffer_m": config.buffer_m,
            "min_overlap_ratio": config.min_overlap_ratio,
            "min_score": config.min_score,
        },
    }

    if report_path is not None:
        _write_report(report_path, report_rows)

    return stats


class _SkdfMatcher:
    def __init__(self, roads: list[SkdfRoad], config: SkdfMatchConfig):
        try:
            from shapely.strtree import STRtree
        except ImportError as exc:
            raise RuntimeError("Package shapely is required for SKDF matching.") from exc

        self.roads = roads
        self.config = config
        self.geometries = [road.geometry for road in roads]
        self.index = STRtree(self.geometries)
        self.geometry_to_road = {id(road.geometry): road for road in roads}

    def match_link(self, link: Link, line: Any) -> LinkMatch | None:
        return self.match_geometry(link.id, link.name, line, source="direct")

    def match_geometry(self, link_id: str, name: str, line: Any, source: str = "direct") -> LinkMatch | None:
        match = self.best_candidate(name, line)
        if match is None:
            return None
        return LinkMatch(
            link_id=link_id,
            road=match.road,
            score=match.score,
            distance_m=match.distance_m,
            overlap_ratio=match.overlap_ratio,
            name_similarity=match.name_similarity,
            source=source,
        )

    def best_candidate(self, name: str, line: Any, respect_acceptance: bool = True) -> LinkMatch | None:
        search_area = line.buffer(self.config.max_distance_m)
        candidates = self.index.query(search_area)
        best: LinkMatch | None = None

        for candidate in candidates:
            road = self._road_from_candidate(candidate)
            if road is None:
                continue

            distance = line.distance(road.geometry)
            if distance > self.config.max_distance_m:
                continue

            overlap_ratio = _safe_ratio(
                line.intersection(road.geometry.buffer(self.config.buffer_m)).length,
                line.length,
            )
            if respect_acceptance and overlap_ratio < self.config.min_overlap_ratio:
                continue

            name_similarity = _name_similarity(name, road.normalized_name)
            if respect_acceptance and (
                self.config.reject_named_mismatches
                and name_similarity < 0
                and not _name_mismatch_override_allowed(distance, overlap_ratio, self.config)
            ):
                continue

            score = _match_score(distance, overlap_ratio, name_similarity, self.config)
            if respect_acceptance and score < self.config.min_score:
                continue

            match = LinkMatch(
                link_id="",
                road=road,
                score=score,
                distance_m=distance,
                overlap_ratio=overlap_ratio,
                name_similarity=name_similarity,
                source="direct",
            )
            if best is None or match.score > best.score:
                best = match

        return best

    def _road_from_candidate(self, candidate: Any) -> SkdfRoad | None:
        if isinstance(candidate, (int,)):
            return self.roads[candidate]

        try:
            import numpy as np

            if isinstance(candidate, np.integer):
                return self.roads[int(candidate)]
        except ImportError:
            pass

        return self.geometry_to_road.get(id(candidate))


def _apply_match(link: Link, match: LinkMatch) -> tuple[bool, bool]:
    road = match.road
    updated_traffic = False
    updated_capacity = False

    if road.traffic is not None:
        link.traffic_counts = {**(link.traffic_counts or {}), "car": road.traffic}
        updated_traffic = True

    link.parameters = dict(link.parameters or {})
    if road.lanes is not None:
        link.parameters["lanes_total"] = max(road.lanes, 1)

    lanes = _int_number(link.parameters.get("lanes_total")) or 1
    if road.capacity is not None:
        link.parameters["capacity_per_lane_base"] = round(road.capacity / max(lanes, 1), 3)
        link.parameters["capacity_total_skdf"] = road.capacity
        updated_capacity = True

    if road.speed_limit is not None:
        link.parameters["speed_limit_skdf"] = road.speed_limit

    link.metadata = {
        **(link.metadata or {}),
        "skdf": {
            "road_id": road.road_id,
            "road_part_id": road.road_part_id,
            "road_name": road.road_name,
            "full_name": road.full_name,
            "traffic": road.traffic,
            "capacity_total": road.capacity,
            "lanes": road.lanes,
            "speed_limit": road.speed_limit,
            "match_score": round(match.score, 4),
            "match_distance_m": round(match.distance_m, 2),
            "match_overlap_ratio": round(match.overlap_ratio, 4),
            "name_similarity": round(match.name_similarity, 4),
            "match_source": match.source,
        },
    }
    link.results = {}
    return updated_traffic, updated_capacity


def _assign_way_group_matches(
    project: Project,
    matcher: _SkdfMatcher,
    link_geometries: dict[str, Any],
    matches: dict[str, LinkMatch],
    best_candidates: dict[str, LinkMatch | None],
) -> None:
    try:
        from shapely.ops import unary_union
    except ImportError as exc:
        raise RuntimeError("Package shapely is required for SKDF matching.") from exc

    links_by_way_id: dict[str, list[Link]] = defaultdict(list)
    for link in project.network.links.values():
        way_id = str((link.metadata or {}).get("osm_way_id") or "").strip()
        if way_id:
            links_by_way_id[way_id].append(link)

    for links in links_by_way_id.values():
        if len(links) < 2:
            continue

        group_name = _group_name(links)
        geometries = [link_geometries.get(link.id) for link in links]
        geometries = [geometry for geometry in geometries if geometry is not None and not geometry.is_empty]
        if len(geometries) < 2:
            continue

        group_geometry = unary_union(geometries)
        if group_geometry is None or group_geometry.is_empty:
            continue

        unmatched_links = [link for link in links if link.id not in matches]
        if not unmatched_links:
            continue

        group_match = matcher.match_geometry(
            link_id=links[0].id,
            name=group_name,
            line=group_geometry,
            source="osm_way_group",
        )

        if group_match is not None:
            for link in unmatched_links:
                line = link_geometries.get(link.id)
                if line is None or line.is_empty:
                    continue
                link_match = _build_match_for_known_road(
                    matcher,
                    link.id,
                    link.name,
                    line,
                    group_match.road,
                    source="osm_way_group",
                )
                if link_match is None:
                    continue
                if link_match.distance_m <= matcher.config.way_group_max_distance_m:
                    matches[link.id] = link_match
                    best_candidates[link.id] = link_match
            continue

        matched_links = [link for link in links if link.id in matches]
        if len(matched_links) < 2:
            continue

        dominant_road_id, dominant_count = Counter(matches[link.id].road.road_id for link in matched_links).most_common(1)[0]
        if dominant_count < 2:
            continue

        dominant_road = matches[matched_links[0].id].road
        for link in matched_links:
            if matches[link.id].road.road_id == dominant_road_id:
                dominant_road = matches[link.id].road
                break

        for link in unmatched_links:
            line = link_geometries.get(link.id)
            if line is None or line.is_empty:
                continue
            link_match = _build_match_for_known_road(
                matcher,
                link.id,
                link.name,
                line,
                dominant_road,
                source="osm_way_propagated",
            )
            if link_match is None:
                continue
            if link_match.distance_m <= matcher.config.way_group_max_distance_m:
                matches[link.id] = link_match
                best_candidates[link.id] = link_match


def _link_geometry_3857(link: Link) -> Any | None:
    try:
        from pyproj import Transformer
        from shapely.geometry import LineString
    except ImportError as exc:
        raise RuntimeError("Packages pyproj and shapely are required for SKDF matching.") from exc

    coords = link.coords or {}
    if coords.get("type") == "polyline":
        points = coords.get("points", [])
    else:
        points = [
            (coords.get("lon_start"), coords.get("lat_start")),
            (coords.get("lon_end"), coords.get("lat_end")),
        ]

    clean_points = [
        (float(lon), float(lat))
        for lon, lat in points
        if lon is not None and lat is not None
    ]
    if len(clean_points) < 2:
        return None

    transformer = _get_4326_to_3857_transformer()
    projected_points = [transformer.transform(lon, lat) for lon, lat in clean_points]
    if projected_points[0] == projected_points[-1]:
        return None
    return LineString(projected_points)


def _get_4326_to_3857_transformer():
    from pyproj import Transformer

    if not hasattr(_get_4326_to_3857_transformer, "_transformer"):
        _get_4326_to_3857_transformer._transformer = Transformer.from_crs(
            "EPSG:4326",
            "EPSG:3857",
            always_xy=True,
        )
    return _get_4326_to_3857_transformer._transformer


def _parse_skdf_geometry(raw: str | None, multilinestring_type: Any) -> Any | None:
    if not raw:
        return None
    try:
        value = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        return None

    lines = []
    for raw_line in value:
        line = []
        for point in raw_line:
            if len(point) < 2:
                continue
            line.append((float(point[0]), float(point[1])))
        if len(line) >= 2:
            lines.append(line)

    if not lines:
        return None
    return multilinestring_type(lines)


def _build_match_for_known_road(
    matcher: _SkdfMatcher,
    link_id: str,
    name: str,
    line: Any,
    road: SkdfRoad,
    source: str,
) -> LinkMatch | None:
    distance = line.distance(road.geometry)
    overlap_ratio = _safe_ratio(
        line.intersection(road.geometry.buffer(matcher.config.buffer_m)).length,
        line.length,
    )
    name_similarity = _name_similarity(name, road.normalized_name)
    score = _match_score(distance, overlap_ratio, name_similarity, matcher.config)
    if name_similarity < 0 and not _name_mismatch_override_allowed(distance, overlap_ratio, matcher.config):
        score = max(score, 0.0)

    return LinkMatch(
        link_id=link_id,
        road=road,
        score=score,
        distance_m=distance,
        overlap_ratio=overlap_ratio,
        name_similarity=name_similarity,
        source=source,
    )


def _match_score(
    distance_m: float,
    overlap_ratio: float,
    name_similarity: float,
    config: SkdfMatchConfig,
) -> float:
    distance_score = max(0.0, 1.0 - distance_m / config.max_distance_m)
    score = 0.65 * overlap_ratio + 0.35 * distance_score
    if name_similarity > 0:
        score += config.name_bonus * name_similarity
    elif name_similarity < 0:
        score -= config.name_mismatch_penalty
    return score


def _strong_geometry_override(distance_m: float, overlap_ratio: float, config: SkdfMatchConfig) -> bool:
    return (
        distance_m <= config.strong_geometry_distance_m
        and overlap_ratio >= config.strong_geometry_overlap_ratio
    )


def _name_mismatch_override_allowed(distance_m: float, overlap_ratio: float, config: SkdfMatchConfig) -> bool:
    return config.allow_strong_geometry_name_override and _strong_geometry_override(distance_m, overlap_ratio, config)


def _name_similarity(link_name: str, skdf_normalized_name: str) -> float:
    link_normalized_name = normalize_road_name(link_name)
    if not link_normalized_name or not skdf_normalized_name:
        return 0.0
    if link_normalized_name.startswith("osm road"):
        return 0.0
    if link_normalized_name == skdf_normalized_name:
        return 1.0
    if link_normalized_name in skdf_normalized_name or skdf_normalized_name in link_normalized_name:
        return 0.75

    link_tokens = set(link_normalized_name.split())
    skdf_tokens = set(skdf_normalized_name.split())
    if not link_tokens or not skdf_tokens:
        return -1.0
    intersection = link_tokens & skdf_tokens
    union = link_tokens | skdf_tokens
    ratio = len(intersection) / len(union)
    if ratio >= 0.5:
        return ratio
    return -1.0


def normalize_road_name(value: str | None) -> str:
    text = _text(value).lower().replace("ё", "е")
    text = re.sub(r"[.,;:()\"'`]", " ", text)
    replacements = {
        "улица": "ул",
        "ул": "ул",
        "проспект": "пр-кт",
        "пр": "пр",
        "переулок": "пер",
        "пер": "пер",
        "шоссе": "ш",
        "ш": "ш",
        "площадь": "пл",
        "пл": "пл",
        "бульвар": "б-р",
        "проезд": "пр-д",
    }
    tokens = [replacements.get(token, token) for token in re.split(r"\s+", text) if token]
    return " ".join(tokens)


def _group_name(links: list[Link]) -> str:
    names = [link.name for link in links if link.name and not link.name.startswith("OSM road")]
    if not names:
        return ""
    return Counter(names).most_common(1)[0][0]


def _report_row(link: Link, match: LinkMatch | None, best_candidate: LinkMatch | None, status: str) -> dict[str, Any]:
    row = {
        "link_id": link.id,
        "link_name": link.name,
        "status": status,
        "match_source": "",
        "road_id": "",
        "road_name": "",
        "score": "",
        "distance_m": "",
        "overlap_ratio": "",
        "name_similarity": "",
        "traffic": "",
        "capacity": "",
        "lanes": "",
        "speed_limit": "",
        "best_candidate_road_id": "",
        "best_candidate_road_name": "",
        "best_candidate_score": "",
        "best_candidate_distance_m": "",
        "best_candidate_overlap_ratio": "",
        "best_candidate_name_similarity": "",
    }
    if best_candidate is not None:
        row.update(
            {
                "best_candidate_road_id": best_candidate.road.road_id,
                "best_candidate_road_name": best_candidate.road.road_name,
                "best_candidate_score": round(best_candidate.score, 4),
                "best_candidate_distance_m": round(best_candidate.distance_m, 2),
                "best_candidate_overlap_ratio": round(best_candidate.overlap_ratio, 4),
                "best_candidate_name_similarity": round(best_candidate.name_similarity, 4),
            }
        )

    if match is None:
        return row

    road = match.road
    row.update(
        {
            "match_source": match.source,
            "road_id": road.road_id,
            "road_name": road.road_name,
            "score": round(match.score, 4),
            "distance_m": round(match.distance_m, 2),
            "overlap_ratio": round(match.overlap_ratio, 4),
            "name_similarity": round(match.name_similarity, 4),
            "traffic": road.traffic if road.traffic is not None else "",
            "capacity": road.capacity if road.capacity is not None else "",
            "lanes": road.lanes if road.lanes is not None else "",
            "speed_limit": road.speed_limit if road.speed_limit is not None else "",
        }
    )
    return row


def _write_report(path: str | Path, rows: list[dict[str, Any]]) -> None:
    path = Path(path)
    fieldnames = [
        "link_id",
        "link_name",
        "status",
        "match_source",
        "road_id",
        "road_name",
        "score",
        "distance_m",
        "overlap_ratio",
        "name_similarity",
        "traffic",
        "capacity",
        "lanes",
        "speed_limit",
        "best_candidate_road_id",
        "best_candidate_road_name",
        "best_candidate_score",
        "best_candidate_distance_m",
        "best_candidate_overlap_ratio",
        "best_candidate_name_similarity",
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _safe_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, min(numerator / denominator, 1.0))


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(value):
            return None
        return float(value)

    text = str(value).strip()
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = []
        if isinstance(parsed, list) and parsed:
            return _number(parsed[0])
        return None
    text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _first_number(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        number = _number(row.get(key))
        if number is not None:
            return number
    return None


def _first_int_number(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        number = _int_number(row.get(key))
        if number is not None:
            return number
    return None


def _int_number(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return max(int(round(number)), 1)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
