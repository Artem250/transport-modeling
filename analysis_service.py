from __future__ import annotations

from demand_assignment_service import DemandAssignmentService
from models import Link, Project
from road_sections import Intersection, StraightRoad


class AnalysisService:
    def __init__(self) -> None:
        self.demand_assignment_service = DemandAssignmentService()

    def analyze_project(self, project: Project, assign_demand: bool = True) -> dict:
        assignment_report = {}
        if assign_demand and self._has_demand_model(project):
            assignment_report = self.demand_assignment_service.assign(project)

        links_report = []

        for link in project.network.links.values():
            section = self._build_section(link, project.pcu_coefficients)
            if section is None:
                continue

            section.analyze_performance()
            optimization_result = section.optimize()

            link.results = {
                **section.analysis_data,
                "id": section.id,
                "name": section.name,
                "type": section.__class__.__name__,
            }

            if optimization_result:
                opt_data = {
                    "Optimization_Proposal": optimization_result["proposal"],
                    "C_optimized": round(optimization_result["C_new"], 0),
                    "VC_optimized": round(optimization_result["vc_new"], 3),
                    "LOS_optimized": optimization_result["los_new"],
                }
                link.results.update(opt_data)

            links_report.append(link.results.copy())

        routes_report = []
        for route in project.network.routes.values():
            route_links = [
                project.network.links[link_id]
                for link_id in route.link_ids
                if link_id in project.network.links
            ]
            if not route_links:
                continue

            total_length_km = sum(link.length_km for link in route_links)
            total_delay_sec = sum(link.results.get("Delay_sec", 0.0) for link in route_links)
            base_speed_kph = 60.0
            base_travel_time_sec = (total_length_km / base_speed_kph) * 3600 if total_length_km else 0.0
            total_travel_time_sec = base_travel_time_sec + total_delay_sec
            avg_speed_kph = (
                total_length_km / (total_travel_time_sec / 3600)
                if total_travel_time_sec > 0
                else base_speed_kph
            )
            route.results = {
                "id": route.id,
                "name": route.name,
                "total_length_km": round(total_length_km, 2),
                "total_delay_sec": round(total_delay_sec, 1),
                "total_travel_time_sec": round(total_travel_time_sec, 1),
                "avg_speed_kph": round(avg_speed_kph, 1),
                "links_detail": [
                    {
                        "link_id": link.id,
                        "LOS": link.results.get("LOS", "UNDEFINED"),
                        "VC_ratio": round(link.results.get("VC_ratio", 0.0), 3),
                        "Delay_sec": round(link.results.get("Delay_sec", 0.0), 1),
                    }
                    for link in route_links
                ],
            }
            routes_report.append(route.results.copy())

        return {
            "Project_Name": project.project_name,
            "Demand_Assignment": assignment_report,
            "Links_Analysis": links_report,
            "Routes_Analysis": routes_report,
        }

    def _has_demand_model(self, project: Project) -> bool:
        if project.demand_model.get("routes"):
            return True
        if project.demand_model.get("turning_coefficients"):
            return True
        return any(route.demand_veh_h > 0 for route in project.network.routes.values())

    def _build_section(self, link: Link, pcu_coeffs: dict[str, float]):
        params = link.parameters
        link_type = link.link_type

        if link_type == "straight":
            return StraightRoad(
                link.id,
                link.name,
                link.traffic_counts,
                pcu_coeffs,
                link.length_km,
                params.get("lanes_total", 1),
                params.get("lanes_bus", 0),
                params.get("capacity_per_lane_base", 1800),
                params.get("lane_width_m", 3.5),
                params.get("grade_percent", 0.0),
                params.get("parking_present", False),
                params.get("heavy_vehicles_percent", 0.0),
            )

        if link_type == "intersection":
            section = Intersection(
                link.id,
                link.name,
                link.traffic_counts,
                pcu_coeffs,
                link.length_km,
                params.get("cycle_time", 100),
                params.get("green_time", 30),
                params.get("saturation_flow_base", 1800),
                params.get("lanes_count", 1),
                params.get("lane_width_m", 3.5),
                params.get("grade_percent", 0.0),
                params.get("parking_present", False),
                params.get("heavy_vehicles_percent", 0.0),
                params.get("is_ring_approach", False),
                params.get("g_others", 0),
            )
            if "g_others" in params:
                section.g_others = params["g_others"]
            return section

        return None
