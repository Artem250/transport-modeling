from __future__ import annotations

import math

from calibration import CalibrationDiagnosticsService
from ctm import CTMSimulator
from los import TARGET_LOS_VC, determine_los
from models import Link, Movement, Project, SimulationConfig, Source
from network_dynamic import Cell, DynamicLink, DynamicNetwork
from network_migration import ensure_dynamic_schema


BASE_SPEED_KPH = 60.0
B_COEFFICIENT = 4.0


class DynamicAnalysisService:
    def __init__(self):
        self.calibration_service = CalibrationDiagnosticsService()

    def analyze_project(self, project: Project) -> dict:
        migration_diagnostics = ensure_dynamic_schema(project)
        runtime_network = self._build_runtime_network(project)
        simulator = CTMSimulator(runtime_network, project.simulation)
        sim_results = simulator.simulate(project.simulation.horizon_seconds)

        links_report = []
        for link in project.network.links.values():
            result = self._build_link_result(project, link, sim_results.get(link.id, {}))
            link.results = result
            links_report.append(result.copy())

        routes_report = self._build_routes_report(project)
        calibration_diagnostics = self.calibration_service.build_diagnostics(project)
        virtual_detector_metrics = self.calibration_service.build_detector_metrics(project)
        project.metadata["calibration_diagnostics"] = calibration_diagnostics
        project.metadata["virtual_detector_metrics"] = virtual_detector_metrics

        return {
            "Project_Name": project.project_name,
            "Links_Analysis": links_report,
            "Routes_Analysis": routes_report,
            "Diagnostics": {
                "migration": migration_diagnostics,
                "calibration": calibration_diagnostics,
                "virtual_detectors": virtual_detector_metrics,
                "runtime": runtime_network.diagnostics,
            },
        }

    def _build_runtime_network(self, project: Project) -> DynamicNetwork:
        diagnostics: list[str] = []
        dynamic_links: dict[str, DynamicLink] = {}
        dt_seconds = self._resolve_dt_seconds(project)

        for link in project.network.links.values():
            params = self._resolve_link_params(project.simulation, link)
            length_m = max(link.length_km * 1000.0, 1.0)
            vf_mps = params["free_flow_speed_kph"] / 3.6
            w_mps = params["wave_speed_kph"] / 3.6
            cfl_length_m = max(vf_mps, w_mps) * dt_seconds
            target_cell_length_m = max(params["target_cell_length_m"], cfl_length_m, 1.0)
            cell_count = max(int(math.floor(length_m / target_cell_length_m)), 1)
            cell_length_m = length_m / cell_count
            if cell_length_m < cfl_length_m:
                diagnostics.append(
                    f"Link {link.id}: CFL condition cannot be satisfied with dt={dt_seconds}s and physical length."
                )

            dynamic_links[link.id] = DynamicLink(
                id=link.id,
                name=link.name,
                start_node_id=link.start_node_id,
                end_node_id=link.end_node_id,
                link_type=link.link_type,
                length_m=length_m,
                lanes=params["lanes"],
                dt_seconds=dt_seconds,
                free_flow_speed_kph=params["free_flow_speed_kph"],
                wave_speed_kph=params["wave_speed_kph"],
                jam_density_pcu_per_km_lane=params["jam_density_pcu_per_km_lane"],
                capacity_pcu_h=params["capacity_pcu_h"],
                cell_length_m=cell_length_m,
                parameters=params,
                metadata=dict(link.metadata),
                cells=[Cell() for _ in range(cell_count)],
            )

        return DynamicNetwork(
            nodes=project.network.nodes,
            links=dynamic_links,
            sources=self._prepare_sources(project),
            sinks=project.network.sinks,
            movements=self._prepare_movements(project.network.movements),
            diagnostics=diagnostics,
        )

    def _resolve_dt_seconds(self, project: Project) -> int:
        config = project.simulation
        dt_seconds = max(config.min_dt_seconds, min(config.dt_seconds, config.max_dt_seconds))
        if not config.adaptive_dt_enabled:
            return dt_seconds

        for link in project.network.links.values():
            params = self._resolve_link_params(config, link)
            max_speed_mps = max(params["free_flow_speed_kph"], params["wave_speed_kph"]) / 3.6
            if max_speed_mps <= 0:
                continue
            reference_length_m = min(
                max(link.length_km * 1000.0, 1.0),
                max(float(params["target_cell_length_m"]), 1.0),
            )
            max_dt_for_link = int(math.floor(reference_length_m / max_speed_mps))
            if max_dt_for_link >= config.min_dt_seconds:
                dt_seconds = min(dt_seconds, max_dt_for_link)

        return max(config.min_dt_seconds, dt_seconds)

    def _prepare_sources(self, project: Project) -> dict[str, Source]:
        prepared: dict[str, Source] = {}
        ratio = min(max(float(project.simulation.directional_split_ratio), 0.0), 1.0)
        for source_id, source in project.network.sources.items():
            demand = dict(source.demand_by_type)
            metadata = dict(source.metadata)
            link = project.network.links.get(source.link_id)
            skdf = (link.metadata or {}).get("skdf", {}) if link is not None else {}
            is_skdf_total = bool(skdf) and not bool(skdf.get("directional", False))
            if source.inferred and is_skdf_total and not metadata.get("directional_split_applied"):
                demand = {vehicle_type: float(value) * ratio for vehicle_type, value in demand.items()}
                metadata["directional_split_applied"] = ratio

            prepared[source_id] = Source(
                id=source.id,
                link_id=source.link_id,
                demand_by_type=demand,
                start_time_s=source.start_time_s,
                end_time_s=source.end_time_s,
                inferred=source.inferred,
                metadata=metadata,
            )
        return prepared

    def _prepare_movements(self, movements: dict[str, Movement]) -> dict[str, Movement]:
        prepared: dict[str, Movement] = {}
        for movement_id, movement in movements.items():
            control = dict(movement.control or {})
            if control.get("control_type") == "signalized":
                phases = []
                for phase in control.get("phases", []):
                    phase_copy = dict(phase)
                    green_for = list(phase_copy.get("green_for_movements", []))
                    if not green_for:
                        green_for = [movement.id]
                    phase_copy["green_for_movements"] = green_for
                    phases.append(phase_copy)
                control["phases"] = phases

            prepared[movement_id] = Movement(
                id=movement.id,
                node_id=movement.node_id,
                from_link_id=movement.from_link_id,
                to_link_id=movement.to_link_id,
                split_ratio=movement.split_ratio,
                capacity_pcu_h=movement.capacity_pcu_h,
                control=control,
                inferred=movement.inferred,
                metadata=dict(movement.metadata),
            )
        return prepared

    def _resolve_link_params(self, config: SimulationConfig, link: Link) -> dict:
        params = {
            "free_flow_speed_kph": config.free_flow_speed_kph,
            "wave_speed_kph": config.wave_speed_kph,
            "jam_density_pcu_per_km_lane": config.jam_density_pcu_per_km_lane,
            "capacity_per_lane_base": config.capacity_per_lane_base,
            "target_cell_length_m": config.target_cell_length_m,
            "observed_pcu_h": _count_total(link.observed_counts or link.traffic_counts),
        }

        for field_name in (
            "free_flow_speed_kph",
            "wave_speed_kph",
            "jam_density_pcu_per_km_lane",
            "capacity_per_lane_base",
            "target_cell_length_m",
            "capacity_total_skdf",
            "capacity_total",
            "capacity_pcu_h",
            "speed_limit_skdf",
            "speed_limit",
        ):
            if field_name in link.parameters:
                params[field_name] = link.parameters[field_name]

        skdf = (link.metadata or {}).get("skdf", {})
        if isinstance(skdf, dict):
            if skdf.get("capacity_total") is not None and "capacity_total_skdf" not in params:
                params["capacity_total_skdf"] = skdf["capacity_total"]
            if skdf.get("speed_limit") is not None and "speed_limit_skdf" not in params:
                params["speed_limit_skdf"] = skdf["speed_limit"]
            if skdf.get("traffic") is not None and not params["observed_pcu_h"]:
                params["observed_pcu_h"] = skdf["traffic"]

        for group_key in self._group_keys(link):
            params.update(config.group_overrides.get(group_key, {}))
        params.update(config.link_overrides.get(link.id, {}))

        lanes = self._effective_lanes(link)
        params["lanes"] = lanes
        if params.get("speed_limit_skdf") is not None:
            params["free_flow_speed_kph"] = max(float(params["speed_limit_skdf"]), 1.0)
        elif params.get("speed_limit") is not None:
            params["free_flow_speed_kph"] = max(float(params["speed_limit"]), 1.0)

        capacity_total = _first_number(
            params.get("capacity_total_skdf"),
            params.get("capacity_total"),
            params.get("capacity_pcu_h"),
        )
        if capacity_total is not None:
            params["capacity_pcu_h"] = max(capacity_total, 0.0)
        elif link.link_type == "intersection":
            saturation = float(link.parameters.get("saturation_flow_base", params["capacity_per_lane_base"]))
            cycle_time = float(link.parameters.get("cycle_time", 0) or 0)
            green_time = float(link.parameters.get("green_time", 0) or 0)
            green_ratio = green_time / cycle_time if cycle_time > 0 and green_time > 0 else 1.0
            params["capacity_pcu_h"] = saturation * lanes * max(green_ratio, 0.1)
        else:
            params["capacity_pcu_h"] = float(params["capacity_per_lane_base"]) * lanes

        return params

    def _group_keys(self, link: Link) -> list[str]:
        keys = [f"link_type:{link.link_type}"]
        for key, value in (link.metadata or {}).items():
            keys.append(f"metadata:{key}={value}")
        return keys

    def _effective_lanes(self, link: Link) -> float:
        params = link.parameters or {}
        lanes = params.get("lanes_count", params.get("lanes_total", 1))
        lanes_bus = params.get("lanes_bus", 0)
        try:
            return max(float(lanes) - float(lanes_bus), 1.0)
        except (TypeError, ValueError):
            return 1.0

    def _build_link_result(self, project: Project, link: Link, sim_result: dict) -> dict:
        avg_flow = float(sim_result.get("avg_flow_pcu_h", 0.0))
        peak_flow = float(sim_result.get("peak_flow_pcu_h", 0.0))
        capacity = max(float(self._resolve_link_params(project.simulation, link)["capacity_pcu_h"]), 0.0)
        vc_ratio = avg_flow / capacity if capacity > 0 else (99.0 if avg_flow > 0 else 0.0)
        los = determine_los(vc_ratio)
        travel_time_sec = float(sim_result.get("avg_travel_time_sec", 0.0))
        delay_sec = float(sim_result.get("delay_sec", 0.0))

        result = {
            "id": link.id,
            "name": link.name,
            "type": link.link_type,
            "avg_flow_pcu_h": round(avg_flow, 2),
            "peak_flow_pcu_h": round(peak_flow, 2),
            "avg_density": round(float(sim_result.get("avg_density_pcu_km", 0.0)), 2),
            "avg_speed_kph": round(float(sim_result.get("avg_speed_kph", BASE_SPEED_KPH)), 2),
            "max_queue_pcu": round(float(sim_result.get("max_queue_pcu", 0.0)), 2),
            "queue_length_m": round(float(sim_result.get("max_queue_length_m", 0.0)), 2),
            "avg_queue_length_m": round(float(sim_result.get("avg_queue_length_m", 0.0)), 2),
            "travel_time_sec": round(travel_time_sec, 2),
            "free_flow_travel_time_sec": round(float(sim_result.get("free_flow_travel_time_sec", 0.0)), 2),
            "throughput_pcu": round(float(sim_result.get("throughput_pcu", 0.0)), 2),
            "demand_served_ratio": round(float(sim_result.get("demand_served_ratio", 1.0)), 3),
            "V": round(avg_flow, 0),
            "C_initial": round(capacity, 0),
            "VC_ratio": round(vc_ratio, 3),
            "LOS": los,
            "Delay_sec": round(delay_sec, 1),
            "Length_km": link.length_km,
        }

        optimization = self._build_optimization(link, avg_flow, capacity, vc_ratio, los)
        if optimization:
            result.update(optimization)
        return result

    def _build_optimization(self, link: Link, avg_flow: float, capacity: float, vc_ratio: float, los: str) -> dict | None:
        if vc_ratio <= TARGET_LOS_VC:
            return None

        required_capacity = avg_flow / TARGET_LOS_VC if TARGET_LOS_VC > 0 else capacity
        if link.link_type == "intersection":
            cycle_time = float(link.parameters.get("cycle_time", 0) or 0)
            green_time = float(link.parameters.get("green_time", 0) or 0)
            saturation = float(link.parameters.get("saturation_flow_base", 1800))
            lanes = self._effective_lanes(link)
            if saturation * lanes > 0 and cycle_time > 0:
                required_green = required_capacity / (saturation * lanes) * cycle_time
                return {
                    "Optimization_Proposal": (
                        f"СВЕТОФОРНОЕ РЕГУЛИРОВАНИЕ: увеличить зелёное время "
                        f"с {green_time:.0f} до {required_green:.1f} сек."
                    ),
                    "C_optimized": round(required_capacity, 0),
                    "VC_optimized": round(TARGET_LOS_VC, 3),
                    "LOS_optimized": determine_los(TARGET_LOS_VC),
                }
        else:
            lanes = self._effective_lanes(link)
            lane_capacity = max(capacity / lanes, 1.0)
            additional_lanes = max(required_capacity / lane_capacity - lanes, 0.1)
            return {
                "Optimization_Proposal": f"РАСШИРЕНИЕ: добавить {additional_lanes:.1f} полосы.",
                "C_optimized": round(required_capacity, 0),
                "VC_optimized": round(TARGET_LOS_VC, 3),
                "LOS_optimized": determine_los(TARGET_LOS_VC),
            }

        return {
            "Optimization_Proposal": "ТРЕБУЕТСЯ РУЧНАЯ КАЛИБРОВКА УПРАВЛЕНИЯ УЗЛОМ.",
            "C_optimized": round(capacity, 0),
            "VC_optimized": round(vc_ratio, 3),
            "LOS_optimized": los,
        }

    def _build_routes_report(self, project: Project) -> list[dict]:
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
            total_travel_time_sec = sum(link.results.get("travel_time_sec", 0.0) for link in route_links)
            avg_speed_kph = (
                total_length_km / (total_travel_time_sec / 3600.0)
                if total_travel_time_sec > 0
                else BASE_SPEED_KPH
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
        return routes_report


def _count_total(counts: dict | None) -> float:
    total = 0.0
    for value in (counts or {}).values():
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue
    return total


def _first_number(*values) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None
