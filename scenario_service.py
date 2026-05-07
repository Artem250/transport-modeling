from __future__ import annotations

from copy import deepcopy

from models import Project, Scenario


class ScenarioService:
    ROUTE_SPLIT_COEFFICIENTS = "route_split_coefficients"
    LEGACY_TURNING_COEFFICIENTS = "turning_coefficients"

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
                    "drives calculated traffic_counts. Use demand scenario changes instead."
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
            route_id = change.get("route_id")
            demand = change.get("demand_value", change.get("demand_veh_h", change.get("demand")))
            route = project.network.routes.get(route_id)
            if route is not None and demand is not None:
                route.demand_value = demand
                return

            for demand_route in project.demand_model.get("routes", []):
                if demand_route.get("id") == route_id and demand is not None:
                    demand_route["demand_value"] = demand
                    demand_route.pop("demand_veh_h", None)
                    demand_route.pop("demand", None)
                    return

        if change_type == "scale_all_route_demand":
            factor = float(change.get("factor", 1.0))
            for route in project.network.routes.values():
                if route.demand_value is not None:
                    route.demand_value *= factor
                else:
                    route.demand_veh_h *= factor

            for demand_route in project.demand_model.get("routes", []):
                if "demand_value" in demand_route:
                    demand_route["demand_value"] *= factor
                elif "demand_veh_h" in demand_route:
                    demand_route["demand_veh_h"] *= factor
                elif "demand" in demand_route:
                    demand_route["demand"] *= factor

            for boundary_id, volume in project.demand_model.get("boundary_flows", {}).items():
                project.demand_model["boundary_flows"][boundary_id] = volume * factor

            for movement in self._iter_route_splits(project):
                if "base_volume_veh_h" in movement:
                    movement["base_volume_veh_h"] *= factor
            return

        if change_type == "reroute":
            route_id = change.get("route_id")
            new_link_ids = change.get("link_ids", change.get("links", []))
            route = project.network.routes.get(route_id)
            if route is not None:
                route.link_ids = new_link_ids
                return

            for demand_route in project.demand_model.get("routes", []):
                if demand_route.get("id") == route_id:
                    demand_route["link_ids"] = new_link_ids
                    return

        if change_type == "update_boundary_flow":
            boundary_id = change.get("boundary_id")
            volume = change.get("volume_veh_h", change.get("volume"))
            if boundary_id is not None:
                project.demand_model.setdefault("boundary_flows", {})[boundary_id] = volume
            return

        if change_type in {"update_route_split_coefficient", "update_turning_coefficient"}:
            movement_id = change.get("movement_id")
            coefficient = change.get("coefficient", change.get("share", change.get("turn_ratio")))
            for movement in self._iter_route_splits(project):
                if movement.get("id") == movement_id and coefficient is not None:
                    movement["coefficient"] = coefficient
                    for demand_key in ("demand_value", "demand_veh_h", "demand"):
                        movement.pop(demand_key, None)
                    return

    def _iter_route_splits(self, project: Project):
        yield from project.demand_model.get(self.ROUTE_SPLIT_COEFFICIENTS, [])
        yield from project.demand_model.get(self.LEGACY_TURNING_COEFFICIENTS, [])
