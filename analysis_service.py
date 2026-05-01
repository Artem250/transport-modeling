from __future__ import annotations

from copy import deepcopy

from dynamic_analysis import DynamicAnalysisService
from models import Project
from static_analysis import StaticAnalysisService


class AnalysisService:
    def __init__(self):
        self.static_service = StaticAnalysisService()
        self.dynamic_service = DynamicAnalysisService()

    def analyze_project(self, project: Project, mode: str | None = None) -> dict:
        selected_mode = (mode or project.analysis_mode or "compare").lower()

        if selected_mode == "static":
            return self.static_service.analyze_project(project)
        if selected_mode == "dynamic":
            return self.dynamic_service.analyze_project(project)
        if selected_mode != "compare":
            raise ValueError(f"Unsupported analysis mode: {selected_mode}")

        static_project = deepcopy(project)
        static_report = self.static_service.analyze_project(static_project)
        dynamic_report = self.dynamic_service.analyze_project(project)
        comparison = self._build_comparison(static_report, dynamic_report)

        dynamic_report["comparison"] = {"static_vs_dynamic": comparison}
        dynamic_report["Static_Analysis"] = static_report

        comparison_by_link_id = {item["link_id"]: item for item in comparison["links"]}
        for link_entry in dynamic_report["Links_Analysis"]:
            link_entry["comparison"] = {
                "static_vs_dynamic": comparison_by_link_id.get(link_entry["id"], {})
            }
            if link_entry["id"] in project.network.links:
                project.network.links[link_entry["id"]].results["comparison"] = link_entry["comparison"]

        return dynamic_report

    def _build_comparison(self, static_report: dict, dynamic_report: dict) -> dict:
        static_links = {item["id"]: item for item in static_report.get("Links_Analysis", [])}
        dynamic_links = {item["id"]: item for item in dynamic_report.get("Links_Analysis", [])}
        link_comparison = []

        for link_id in sorted(set(static_links) | set(dynamic_links)):
            static_link = static_links.get(link_id, {})
            dynamic_link = dynamic_links.get(link_id, {})
            link_comparison.append(
                {
                    "link_id": link_id,
                    "static_los": static_link.get("LOS"),
                    "dynamic_los": dynamic_link.get("LOS"),
                    "static_vc": static_link.get("VC_ratio"),
                    "dynamic_vc": dynamic_link.get("VC_ratio"),
                    "static_delay_sec": static_link.get("Delay_sec"),
                    "dynamic_delay_sec": dynamic_link.get("Delay_sec"),
                    "delta_vc": _round_delta(dynamic_link.get("VC_ratio"), static_link.get("VC_ratio")),
                    "delta_delay_sec": _round_delta(dynamic_link.get("Delay_sec"), static_link.get("Delay_sec")),
                }
            )

        return {"links": link_comparison}


def _round_delta(new_value, old_value) -> float | None:
    if new_value is None or old_value is None:
        return None
    return round(float(new_value) - float(old_value), 3)
