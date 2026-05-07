from __future__ import annotations

from copy import deepcopy
from typing import Any

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

        if change_type in {"update_traffic", "update_parameters", "disable_link", "update_length"}:
            if link is None:
                self._warn(project, f"Scenario change {change_type} ignored: link_id {link_id} not found.")
                return

        if change_type == "update_traffic":
            if project.demand_model:
                self._warn(
                    project,
                    f"update_traffic for link {link.id} ignored because demand_model "
                    "drives calculated traffic_counts. Change demand_model instead.",
                )
                return
            link.traffic_counts.update(change.get("traffic_counts", {}))
            return

        if change_type == "update_parameters":
            parameters = change.get("parameters")
            if not isinstance(parameters, dict):
                self._warn(project, f"Scenario change update_parameters ignored for {link.id}: parameters missing.")
                return
            link.parameters.update(parameters)
            return

        if change_type == "disable_link":
            link.metadata["disabled"] = True
            link.parameters["capacity_per_lane_base"] = 0
            return

        if change_type == "update_length":
            length = self._float_value(project, change.get("length_km"), f"update_length {link.id} length_km")
            if length is None:
                return
            link.length_km = length
            return

        handlers = {
            "update_route_demand": self._update_route_demand,
            "scale_all_route_demand": self._scale_all_route_demand,
            "update_boundary_flow": self._update_boundary_flow,
            "update_route_split_coefficient": self._update_route_split_coefficient,
            "reroute": self._reroute,
        }
        handler = handlers.get(change_type)
        if handler is None:
            self._warn(project, f"Scenario change ignored: unsupported type {change_type}.")
            return
        handler(project, change)

    def _update_route_demand(self, project: Project, change: dict) -> None:
        route_id = change.get("route_id")
        demand = self._float_value(project, change.get("demand_value"), "update_route_demand demand_value")
        if route_id is None or demand is None:
            self._warn(project, "Scenario change update_route_demand ignored: route_id or demand_value missing or invalid.")
            return

        for demand_route in project.demand_model.get("routes", []):
            if demand_route.get("id") == route_id:
                demand_route["demand_value"] = demand
                return

        self._warn(
            project,
            f"Scenario change update_route_demand ignored: demand_model route_id {route_id} not found.",
        )

    def _scale_all_route_demand(self, project: Project, change: dict) -> None:
        factor = self._float_value(project, change.get("factor", 1.0), "scale_all_route_demand factor")
        if factor is None:
            return

        demand_model_type = project.demand_model.get("type")
        if demand_model_type == "routes":
            for demand_route in project.demand_model.get("routes", []):
                if "demand_value" in demand_route:
                    demand = self._float_value(project, demand_route["demand_value"], f"route {demand_route.get('id')} demand_value")
                    if demand is not None:
                        demand_route["demand_value"] = demand * factor
            return

        if demand_model_type == "route_split_coefficients":
            boundary_flows = project.demand_model.get("boundary_flows", {})
            for boundary_id, volume in list(boundary_flows.items()):
                numeric_volume = self._float_value(project, volume, f"boundary_flow {boundary_id}")
                if numeric_volume is not None:
                    boundary_flows[boundary_id] = numeric_volume * factor
            return

        self._warn(
            project,
            "Scenario change scale_all_route_demand ignored: project has no supported demand_model.",
        )

    def _update_boundary_flow(self, project: Project, change: dict) -> None:
        boundary_id = change.get("boundary_id")
        volume = self._float_value(
            project,
            change.get("volume", change.get("volume_veh_h")),
            "update_boundary_flow volume",
        )
        if boundary_id is None or volume is None:
            self._warn(project, "Scenario change update_boundary_flow ignored: boundary_id or volume missing or invalid.")
            return
        if boundary_id not in project.network.nodes:
            self._warn(project, f"Scenario change update_boundary_flow ignored: boundary node {boundary_id} not found.")
            return
        project.demand_model.setdefault("boundary_flows", {})[boundary_id] = volume

    def _update_route_split_coefficient(self, project: Project, change: dict) -> None:
        movement_id = change.get("movement_id")
        coefficient = self._float_value(project, change.get("coefficient"), "update_route_split_coefficient coefficient")
        if movement_id is None or coefficient is None:
            self._warn(project, "Scenario change update_route_split_coefficient ignored: movement_id or coefficient missing or invalid.")
            return

        for movement in project.demand_model.get("route_split_coefficients", []):
            if movement.get("id") == movement_id:
                movement["coefficient"] = coefficient
                return
        self._warn(project, f"Scenario change update_route_split_coefficient ignored: movement_id {movement_id} not found.")

    def _reroute(self, project: Project, change: dict) -> None:
        route_id = change.get("route_id")
        if route_id is None or "link_ids" not in change:
            self._warn(project, "Scenario change reroute ignored: route_id or link_ids missing.")
            return
        new_link_ids = change["link_ids"]
        if not isinstance(new_link_ids, list):
            self._warn(project, f"Scenario change reroute ignored for {route_id}: link_ids must be a list.")
            return

        for demand_route in project.demand_model.get("routes", []):
            if demand_route.get("id") == route_id:
                demand_route["link_ids"] = new_link_ids
                return

        for split in project.demand_model.get("route_split_coefficients", []):
            if split.get("id") == route_id:
                split["link_ids"] = new_link_ids
                return

        self._warn(
            project,
            f"Scenario change reroute ignored: demand_model route/split id {route_id} not found.",
        )

    def _float_value(self, project: Project, value: Any, label: str) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            self._warn(project, f"Scenario change ignored: {label} must be a number.")
            return None

    def _warn(self, project: Project, message: str) -> None:
        project.metadata.setdefault("scenario_warnings", []).append(message)
