from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class QualityConfig:
    strong_distance_m: float = 5.0
    strong_overlap_ratio: float = 0.85
    review_limit: int = 20


@dataclass(frozen=True)
class LinkInfo:
    link_id: str
    name: str
    length_km: float
    traffic: float | None
    capacity: float | None
    los: str
    vc_ratio: float | None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a data quality report for an OSM project enriched with SKDF data."
    )
    parser.add_argument("--project", default="osm_network_project_skdf_v3.json", help="Enriched project JSON path.")
    parser.add_argument("--match-report", default="skdf_match_report_v3.csv", help="SKDF matching CSV report.")
    parser.add_argument("--output", default="data_quality_report.md", help="Output Markdown report path.")
    parser.add_argument("--review-limit", type=int, default=20, help="Maximum number of review cases per section.")
    args = parser.parse_args()

    config = QualityConfig(review_limit=args.review_limit)
    project_data = _read_json(args.project)
    links = _load_links(project_data)
    rows = _read_match_rows(args.match_report)
    report = build_quality_report(project_data, links, rows, config)
    output_path = Path(args.output)
    output_path.write_text(report, encoding="utf-8")
    print(f"Saved data quality report: {output_path.resolve()}")


def build_quality_report(
    project_data: dict[str, Any],
    links: dict[str, LinkInfo],
    rows: list[dict[str, str]],
    config: QualityConfig,
) -> str:
    total_rows = len(rows)
    status_counts = Counter(row.get("status", "") for row in rows)
    source_counts = Counter(row.get("match_source", "") or "not_matched" for row in rows)
    matched_rows = [row for row in rows if _is_matched(row)]
    unmatched_rows = [row for row in rows if not _is_matched(row)]
    ready_rows = [row for row in matched_rows if _has_number(row.get("traffic")) and _has_number(row.get("capacity"))]
    matched_length = _sum_length(matched_rows, links)
    ready_length = _sum_length(ready_rows, links)
    total_length = sum(link.length_km for link in links.values())
    metadata = project_data.get("metadata", {}).get("skdf_enrichment", {})

    name_conflicts = _strong_name_conflicts(unmatched_rows, config)
    weak_geometry = _weak_geometry_cases(unmatched_rows, config)
    missing_candidates = [row for row in unmatched_rows if not row.get("best_candidate_road_id")]
    missing_values = [
        row
        for row in matched_rows
        if not (_has_number(row.get("traffic")) and _has_number(row.get("capacity")))
    ]
    overloaded = _overloaded_links(links)

    lines = [
        "# Отчёт о качестве подготовки данных для транспортного моделирования",
        "",
        "## Постановка прикладной проблемы",
        "",
        (
            "Для расчёта загрузки дорожной сети недостаточно одной картографической геометрии: "
            "OSM хорошо описывает топологию и координаты, но обычно не содержит измеренных "
            "интенсивностей и пропускной способности. Данные СКДФ содержат эксплуатационные "
            "характеристики, но имеют другую сегментацию и не могут быть напрямую использованы "
            "как готовый граф транспортной модели. Поэтому ключевая проблема проекта - "
            "автоматизировать сопоставление разнородных дорожных данных, оценить достоверность "
            "сопоставления и выделить участки, требующие ручной проверки."
        ),
        "",
        "## Сводные показатели",
        "",
        f"- Участков OSM в проекте: {total_rows}",
        f"- Участков, сопоставленных с СКДФ: {len(matched_rows)} ({_pct(len(matched_rows), total_rows)})",
        f"- Участков, готовых к расчёту по интенсивности и пропускной способности: {len(ready_rows)} ({_pct(len(ready_rows), total_rows)})",
        f"- Длина сети в проекте: {total_length:.2f} км",
        f"- Длина сопоставленной части сети: {matched_length:.2f} км ({_pct(matched_length, total_length)})",
        f"- Длина части сети с полными расчётными параметрами: {ready_length:.2f} км ({_pct(ready_length, total_length)})",
        f"- Загружено объектов СКДФ: {metadata.get('skdf_roads_loaded', 'нет данных')}",
        "",
        "## Типы результатов сопоставления",
        "",
        _format_counter(status_counts),
        "",
        "## Источники принятых сопоставлений",
        "",
        _format_counter(source_counts),
        "",
        "## Что система выявляет автоматически",
        "",
        (
            f"- Сильные геометрические совпадения с конфликтом названий: {len(name_conflicts)}. "
            "Это кандидаты на ручную проверку, а не на автоматическое принятие."
        ),
        (
            f"- Несопоставленные участки со слабой геометрической близостью: {len(weak_geometry)}. "
            "Для них официальная линия либо отсутствует рядом, либо отличается по трассировке."
        ),
        (
            f"- Участки без найденного кандидата СКДФ: {len(missing_candidates)}. "
            "Они показывают пробел покрытия входных данных."
        ),
        f"- Сопоставленные участки без полного набора traffic/capacity: {len(missing_values)}.",
        f"- Участки с расчётным LOS E/F или V/C > 1: {len(overloaded)}.",
        "",
    ]

    lines.extend(
        _case_section(
            "Сильные геометрические совпадения с конфликтом названий",
            name_conflicts,
            links,
            config.review_limit,
            reason="Такие случаи опасно принимать автоматически: геометрия совпадает, но название OSM и СКДФ расходится.",
        )
    )
    lines.extend(
        _case_section(
            "Несопоставленные участки со слабой геометрией",
            weak_geometry,
            links,
            config.review_limit,
            reason="Эти участки требуют уточнения геометрии, расширения выгрузки СКДФ или ручного назначения данных.",
        )
    )
    lines.extend(_overload_section(overloaded, config.review_limit))
    lines.extend(
        [
            "## Вывод для ВКР",
            "",
            (
                "Текущий результат следует рассматривать не как аналог картографического сервиса, "
                "а как прототип подсистемы подготовки входных данных для транспортного моделирования. "
                "Её практическая ценность в том, что она переводит неструктурированную ручную работу "
                "по связыванию OSM и СКДФ в воспроизводимый процесс: импорт, геометрическое и "
                "семантическое сопоставление, обогащение графа, отчёт о покрытии и список спорных "
                "участков для эксперта."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _read_json(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_match_rows(path: str | Path) -> list[dict[str, str]]:
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def _load_links(project_data: dict[str, Any]) -> dict[str, LinkInfo]:
    links = {}
    for item in project_data.get("network", {}).get("links", []):
        results = item.get("results") or {}
        traffic_counts = item.get("traffic_counts") or {}
        parameters = item.get("parameters") or {}
        links[item["id"]] = LinkInfo(
            link_id=item["id"],
            name=item.get("name", item["id"]),
            length_km=_float(item.get("length_km")) or 0.0,
            traffic=_float(traffic_counts.get("car")),
            capacity=_link_capacity(parameters),
            los=str(results.get("LOS") or ""),
            vc_ratio=_float(results.get("VC_ratio")),
        )
    return links


def _link_capacity(parameters: dict[str, Any]) -> float | None:
    total = _float(parameters.get("capacity_total_skdf") or parameters.get("capacity_total"))
    if total is not None:
        return total
    base = _float(parameters.get("capacity_per_lane_base") or parameters.get("saturation_flow_base"))
    lanes = _float(parameters.get("lanes_total") or parameters.get("lanes_count") or 1)
    if base is None or lanes is None:
        return None
    return base * lanes


def _is_matched(row: dict[str, str]) -> bool:
    return row.get("status", "").startswith("matched_")


def _strong_name_conflicts(rows: list[dict[str, str]], config: QualityConfig) -> list[dict[str, str]]:
    conflicts = []
    for row in rows:
        distance = _float(row.get("best_candidate_distance_m"))
        overlap = _float(row.get("best_candidate_overlap_ratio"))
        name_similarity = _float(row.get("best_candidate_name_similarity"))
        if distance is None or overlap is None or name_similarity is None:
            continue
        if (
            distance <= config.strong_distance_m
            and overlap >= config.strong_overlap_ratio
            and name_similarity < 0
        ):
            conflicts.append(row)
    return _sort_by_candidate_score(conflicts)


def _weak_geometry_cases(rows: list[dict[str, str]], config: QualityConfig) -> list[dict[str, str]]:
    cases = []
    for row in rows:
        if not row.get("best_candidate_road_id"):
            continue
        distance = _float(row.get("best_candidate_distance_m"))
        overlap = _float(row.get("best_candidate_overlap_ratio"))
        if distance is None or overlap is None:
            continue
        if distance > config.strong_distance_m or overlap < config.strong_overlap_ratio:
            cases.append(row)
    return _sort_by_candidate_score(cases)


def _overloaded_links(links: dict[str, LinkInfo]) -> list[LinkInfo]:
    result = []
    for link in links.values():
        if link.los in {"E", "F"}:
            result.append(link)
            continue
        if link.vc_ratio is not None and link.vc_ratio > 1.0:
            result.append(link)
    return sorted(result, key=lambda item: item.vc_ratio or 0.0, reverse=True)


def _case_section(
    title: str,
    rows: list[dict[str, str]],
    links: dict[str, LinkInfo],
    limit: int,
    reason: str,
) -> list[str]:
    lines = [f"## {title}", "", reason, ""]
    if not rows:
        lines.extend(["Нет случаев для вывода.", ""])
        return lines
    lines.append("| link_id | OSM name | SKDF candidate | distance, m | overlap | score |")
    lines.append("|---|---|---|---:|---:|---:|")
    for row in rows[:limit]:
        link = links.get(row.get("link_id", ""))
        osm_name = link.name if link else row.get("link_name", "")
        lines.append(
            "| {link_id} | {osm_name} | {road_name} | {distance} | {overlap} | {score} |".format(
                link_id=row.get("link_id", ""),
                osm_name=_escape_md(osm_name),
                road_name=_escape_md(row.get("best_candidate_road_name", "")),
                distance=row.get("best_candidate_distance_m", ""),
                overlap=row.get("best_candidate_overlap_ratio", ""),
                score=row.get("best_candidate_score", ""),
            )
        )
    lines.append("")
    return lines


def _overload_section(links: list[LinkInfo], limit: int) -> list[str]:
    lines = ["## Расчётно перегруженные участки", ""]
    if not links:
        lines.extend(["Нет участков с LOS E/F или V/C > 1.", ""])
        return lines
    lines.append("| link_id | name | length, km | LOS | V/C |")
    lines.append("|---|---|---:|---:|---:|")
    for link in links[:limit]:
        vc_ratio = "" if link.vc_ratio is None else f"{link.vc_ratio:.3f}"
        lines.append(
            f"| {link.link_id} | {_escape_md(link.name)} | {link.length_km:.3f} | {link.los} | {vc_ratio} |"
        )
    lines.append("")
    return lines


def _sort_by_candidate_score(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(rows, key=lambda row: _float(row.get("best_candidate_score")) or -999.0, reverse=True)


def _sum_length(rows: list[dict[str, str]], links: dict[str, LinkInfo]) -> float:
    return sum(links[row["link_id"]].length_km for row in rows if row.get("link_id") in links)


def _format_counter(counter: Counter[str]) -> str:
    if not counter:
        return "Нет данных."
    lines = ["| Категория | Количество |", "|---|---:|"]
    for key, value in sorted(counter.items(), key=lambda item: item[1], reverse=True):
        name = key or "not_matched"
        lines.append(f"| {_escape_md(name)} | {value} |")
    return "\n".join(lines)


def _pct(value: float, total: float) -> str:
    if total <= 0:
        return "0.0%"
    return f"{value / total * 100:.1f}%"


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_number(value: Any) -> bool:
    return _float(value) is not None


def _escape_md(value: str) -> str:
    return str(value).replace("|", "\\|")


if __name__ == "__main__":
    main()
