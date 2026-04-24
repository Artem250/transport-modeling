from __future__ import annotations

import argparse

from project_loader import ProjectLoader
from project_saver import ProjectSaver
from skdf_matcher import SkdfMatchConfig, enrich_project_with_skdf


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assign SKDF traffic and capacity data to OSM project links."
    )
    parser.add_argument(
        "--project",
        default="osm_network_project.json",
        help="Input project JSON path.",
    )
    parser.add_argument(
        "--skdf-csv",
        default="nsk_roads_bbox.csv",
        help="SKDF CSV path exported by api_test.py.",
    )
    parser.add_argument(
        "--output",
        default="osm_network_project_skdf.json",
        help="Output enriched project JSON path.",
    )
    parser.add_argument(
        "--report",
        default="skdf_match_report.csv",
        help="Output per-link matching report CSV path.",
    )
    parser.add_argument(
        "--max-distance-m",
        type=float,
        default=35.0,
        help="Maximum distance from an OSM link to an SKDF road geometry.",
    )
    parser.add_argument(
        "--buffer-m",
        type=float,
        default=25.0,
        help="Buffer around SKDF roads used to estimate overlap with OSM links.",
    )
    parser.add_argument(
        "--min-overlap-ratio",
        type=float,
        default=0.45,
        help="Minimum part of an OSM link that must lie near an SKDF road.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.55,
        help="Minimum combined geometry/name score for accepting a match.",
    )
    parser.add_argument(
        "--allow-name-mismatches",
        action="store_true",
        help="Allow geometry matches even when OSM and SKDF road names conflict.",
    )
    parser.add_argument(
        "--allow-name-mismatch-overrides",
        action="store_true",
        help="Allow very strong geometry matches to override conflicting road names.",
    )
    args = parser.parse_args()

    project = ProjectLoader().load(args.project)
    config = SkdfMatchConfig(
        max_distance_m=args.max_distance_m,
        buffer_m=args.buffer_m,
        min_overlap_ratio=args.min_overlap_ratio,
        min_score=args.min_score,
        reject_named_mismatches=not args.allow_name_mismatches,
        allow_strong_geometry_name_override=args.allow_name_mismatch_overrides,
    )
    stats = enrich_project_with_skdf(
        project,
        args.skdf_csv,
        config=config,
        report_path=args.report,
    )
    ProjectSaver().save(project, args.output)

    print(f"SKDF roads loaded: {stats.skdf_roads_loaded}")
    print(f"Links total: {stats.links_total}")
    print(f"Links with geometry: {stats.links_with_geometry}")
    print(f"Links matched: {stats.links_matched}")
    print(f"Links updated with traffic: {stats.links_updated_traffic}")
    print(f"Links updated with capacity: {stats.links_updated_capacity}")
    print(f"Saved enriched project: {args.output}")
    print(f"Saved matching report: {args.report}")


if __name__ == "__main__":
    main()
