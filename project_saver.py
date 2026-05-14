from __future__ import annotations

import json
from pathlib import Path

from models import Project


class ProjectSaver:
    def save(self, project: Project, path: str | Path) -> None:
        data = {
            "project_name": project.project_name,
            "pcu_coefficients": project.pcu_coefficients,
            "analysis_mode": project.analysis_mode,
            "simulation": {
                "horizon_seconds": project.simulation.horizon_seconds,
                "dt_seconds": project.simulation.dt_seconds,
                "min_dt_seconds": project.simulation.min_dt_seconds,
                "max_dt_seconds": project.simulation.max_dt_seconds,
                "target_cell_length_m": project.simulation.target_cell_length_m,
                "adaptive_dt_enabled": project.simulation.adaptive_dt_enabled,
                "free_flow_speed_kph": project.simulation.free_flow_speed_kph,
                "wave_speed_kph": project.simulation.wave_speed_kph,
                "jam_density_pcu_per_km_lane": project.simulation.jam_density_pcu_per_km_lane,
                "capacity_per_lane_base": project.simulation.capacity_per_lane_base,
                "split_update_interval_s": project.simulation.split_update_interval_s,
                "split_inertia_alpha": project.simulation.split_inertia_alpha,
                "congestion_speed_penalty_power": project.simulation.congestion_speed_penalty_power,
                "directional_split_ratio": project.simulation.directional_split_ratio,
                "group_overrides": project.simulation.group_overrides,
                "link_overrides": project.simulation.link_overrides,
                "metadata": project.simulation.metadata,
            },
            "metadata": project.metadata,
            "network": {
                "nodes": [
                    {
                        "id": node.id,
                        "lon": node.lon,
                        "lat": node.lat,
                        "x": node.x,
                        "y": node.y,
                        "node_type": node.node_type,
                        "name": node.name,
                        "metadata": node.metadata,
                    }
                    for node in project.network.nodes.values()
                ],
                "links": [
                    {
                        "id": link.id,
                        "name": link.name,
                        "start_node_id": link.start_node_id,
                        "end_node_id": link.end_node_id,
                        "link_type": link.link_type,
                        "length_km": link.length_km,
                        "traffic_counts": link.traffic_counts,
                        "observed_counts": link.observed_counts,
                        "coords": link.coords,
                        "parameters": link.parameters,
                        "results": link.results,
                        "metadata": link.metadata,
                    }
                    for link in project.network.links.values()
                ],
                "routes": [
                    {
                        "id": route.id,
                        "name": route.name,
                        "link_ids": route.link_ids,
                        "results": route.results,
                    }
                    for route in project.network.routes.values()
                ],
                "sources": [
                    {
                        "id": source.id,
                        "link_id": source.link_id,
                        "demand_by_type": source.demand_by_type,
                        "start_time_s": source.start_time_s,
                        "end_time_s": source.end_time_s,
                        "inferred": source.inferred,
                        "metadata": source.metadata,
                    }
                    for source in project.network.sources.values()
                ],
                "sinks": [
                    {
                        "id": sink.id,
                        "link_id": sink.link_id,
                        "capacity_pcu_h": sink.capacity_pcu_h,
                        "inferred": sink.inferred,
                        "metadata": sink.metadata,
                    }
                    for sink in project.network.sinks.values()
                ],
                "movements": [
                    {
                        "id": movement.id,
                        "node_id": movement.node_id,
                        "from_link_id": movement.from_link_id,
                        "to_link_id": movement.to_link_id,
                        "split_ratio": movement.split_ratio,
                        "capacity_pcu_h": movement.capacity_pcu_h,
                        "control": movement.control,
                        "inferred": movement.inferred,
                        "metadata": movement.metadata,
                    }
                    for movement in project.network.movements.values()
                ],
            },
            "scenarios": [
                {
                    "id": scenario.id,
                    "name": scenario.name,
                    "description": scenario.description,
                    "changes": scenario.changes,
                    "results_snapshot": scenario.results_snapshot,
                }
                for scenario in project.scenarios
            ],
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
