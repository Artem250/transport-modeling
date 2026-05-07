from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from models import Link, Project, Route


class ProjectSaver:
    def save(self, project: Project, path: str | Path) -> None:
        data = {
            "project_name": project.project_name,
            "pcu_coefficients": project.pcu_coefficients,
            "demand_model": self._serialize_demand_model(project.demand_model),
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
                        "traffic_counts": self._serialize_link_traffic_counts(project, link),
                        "coords": link.coords,
                        "parameters": link.parameters,
                        "metadata": self._serialize_link_metadata(link),
                    }
                    for link in project.network.links.values()
                ],
                "routes": [self._serialize_route(route) for route in project.network.routes.values()],
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

    def _serialize_demand_model(self, demand_model: dict) -> dict:
        serialized = deepcopy(demand_model or {})
        for key in ("routes", "route_split_coefficients"):
            if isinstance(serialized.get(key), list):
                serialized[key] = [
                    self._clean_runtime_fields(item) for item in serialized[key]
                ]
        return serialized

    def _serialize_link_traffic_counts(self, project: Project, link: Link) -> dict:
        if (
            project.demand_model
            and link.metadata.get("traffic_counts_source") == "assigned_demand"
        ):
            return dict(link.metadata.get("observed_traffic_counts", {}))
        return dict(link.traffic_counts)

    def _serialize_link_metadata(self, link: Link) -> dict:
        metadata = dict(link.metadata)
        if metadata.get("traffic_counts_source") == "assigned_demand":
            metadata.pop("traffic_counts_source", None)
            metadata.pop("observed_traffic_counts", None)
        metadata.pop("source_traffic_counts", None)
        return self._clean_runtime_fields(metadata)

    def _serialize_route(self, route: Route) -> dict:
        data = {
            "id": route.id,
            "name": route.name,
            "link_ids": route.link_ids,
            "origin_node_id": route.origin_node_id,
            "destination_node_id": route.destination_node_id,
            "vehicle_type": route.vehicle_type,
            "metadata": self._clean_runtime_fields(route.metadata),
        }
        return data

    def _clean_runtime_fields(self, item: dict) -> dict:
        cleaned = dict(item)
        for key in (
            "assigned_link_ids",
            "results",
            "demand_veh_h",
            "demand",
        ):
            cleaned.pop(key, None)
        return cleaned
