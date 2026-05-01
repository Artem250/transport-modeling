from __future__ import annotations

from copy import deepcopy

from models import Project, Scenario


class ScenarioService:
    def apply_scenario(self, project: Project, scenario: Scenario) -> Project:
        scenario_project = deepcopy(project)
        for change in scenario.changes:
            self._apply_change(scenario_project, change)
        return scenario_project

    def _apply_change(self, project: Project, change: dict) -> None:
        change_type = change.get("type")
        link_id = change.get("link_id")
        link = project.network.links.get(link_id) if link_id else None

        if change_type == "update_traffic" and link is not None:
            source = next((item for item in project.network.sources.values() if item.link_id == link.id), None)
            if source is not None:
                source.demand_by_type.update(change.get("traffic_counts", {}))
            else:
                link.traffic_counts.update(change.get("traffic_counts", {}))
            return

        if change_type == "update_source_demand":
            source_id = change.get("source_id")
            source = project.network.sources.get(source_id) if source_id else None
            if source is not None:
                source.demand_by_type.update(change.get("demand_by_type", {}))
            return

        if change_type == "update_movement_split":
            movement_id = change.get("movement_id")
            movement = project.network.movements.get(movement_id) if movement_id else None
            if movement is not None and "split_ratio" in change:
                movement.split_ratio = change["split_ratio"]
            return

        if change_type == "update_signal_control":
            movement_id = change.get("movement_id")
            movement = project.network.movements.get(movement_id) if movement_id else None
            if movement is not None:
                movement.control.update(change.get("control", {}))
            return

        if change_type == "update_parameters" and link is not None:
            link.parameters.update(change.get("parameters", {}))
            return

        if change_type == "disable_link" and link is not None:
            link.metadata["disabled"] = True
            link.parameters["capacity_per_lane_base"] = 0
            return

        if change_type == "update_length" and link is not None:
            link.length_km = change.get("length_km", link.length_km)
