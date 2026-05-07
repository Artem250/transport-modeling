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
            if project.demand_model:
                project.metadata.setdefault("scenario_warnings", []).append(
                    f"update_traffic for link {link.id} ignored because demand_model "
                    "drives calculated traffic_counts. Change demand_model instead."
                )
                return
            link.traffic_counts.update(change.get("traffic_counts", {}))
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
            return

        if change_type == "update_route_demand":
            self._update_route_demand(project, change)
            return

        if change_type == "scale_all_route_demand":
            self._scale_all_route_demand(project, float(change.get("factor", 1.0)))
            return

        if change_type == "update_boundary_flow":
            boundary_id = change.get("boundary_id")
            volume = change.get("volume", change.get("volume_veh_h"))
            if boundary_id is not None and volume is not None:
                project.demand_model.setdefault("boundary_flows", {})[boundary_id] = volume
            return

        if change_type == "update_route_split_coefficient":
            self._update_route_split_coefficient(project, change)
            return

        if change_type == "reroute":
            self._reroute(project, change)
            return

    def _update_route_demand(self, project: Project, change: dict) -> None:
        route_id = change.get("route_id")
        demand = change.get("demand_value")
        if route_id is None or demand is None:
            return

        for demand_route in project.demand_model.get("routes", []):
            if demand_route.get("id") == route_id:
                demand_route["demand_value"] = demand
                return

        route = project.network.routes.get(route_id)
        if route is not None:
            route.demand_value = demand

    def _scale_all_route_demand(self, project: Project, factor: float) -> None:
        demand_model_type = project.demand_model.get("type")
        if demand_model_type == "routes":
            for demand_route in project.demand_model.get("routes", []):
                if "demand_value" in demand_route:
                    demand_route["demand_value"] *= factor
            return

        if demand_model_type == "route_split_coefficients":
            boundary_flows = project.demand_model.get("boundary_flows", {})
            for boundary_id, volume in list(boundary_flows.items()):
                boundary_flows[boundary_id] = volume * factor
            return

        for route in project.network.routes.values():
            if route.demand_value is not None:
                route.demand_value *= factor

    def _update_route_split_coefficient(self, project: Project, change: dict) -> None:
        movement_id = change.get("movement_id")
        coefficient = change.get("coefficient")
        if movement_id is None or coefficient is None:
            return

        for movement in project.demand_model.get("route_split_coefficients", []):
            if movement.get("id") == movement_id:
                movement["coefficient"] = coefficient
                return

    def _reroute(self, project: Project, change: dict) -> None:
        route_id = change.get("route_id")
        new_link_ids = change.get("link_ids", [])
        if route_id is None:
            return

        for demand_route in project.demand_model.get("routes", []):
            if demand_route.get("id") == route_id:
                demand_route["link_ids"] = new_link_ids
                return

        for split in project.demand_model.get("route_split_coefficients", []):
            if split.get("id") == route_id:
                split["link_ids"] = new_link_ids
                return

        route = project.network.routes.get(route_id)
        if route is not None:
            route.link_ids = new_link_ids
