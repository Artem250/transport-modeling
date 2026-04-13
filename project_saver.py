from __future__ import annotations

import json
from pathlib import Path

from models import Project


class ProjectSaver:
    def save(self, project: Project, path: str | Path) -> None:
        data = {
            "project_name": project.project_name,
            "pcu_coefficients": project.pcu_coefficients,
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

