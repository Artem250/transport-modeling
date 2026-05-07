from __future__ import annotations

from collections import defaultdict
from typing import Any

from models import Network, Project
from routing_service import RoutingService


class DemandAssignmentService:
    """Assigns demand to links using one explicit demand model mode."""

    MODE_ROUTES = "routes"
    MODE_NETWORK_ROUTES = "network_routes"
    MODE_ROUTE_SPLITS = "route_split_coefficients"
    LEGACY_ROUTE_SPLITS = "turning_coefficients"

    def __init__(self) -> None:
        self.routing_service = RoutingService()

    def assign(self, project: Project) -> dict[str, Any]:
        warnings = []
        demand_mode, mode_warnings = self._resolve_demand_mode(project)
        warnings.extend(mode_warnings)

        if demand_mode is None:
            return {
                "demand_model_type": None,
                "demand_unit": self._get_demand_unit(project),
                "available_routes": 0,
                "assigned_routes": 0,
                "warnings": warnings,
            }

        self._prepare_assigned_counts(project)

        routes = self._collect_routes(project, demand_mode, warnings)
        assigned_routes = 0

        for route in routes:
            route_id = route.get("id")
            link_ids = list(route.get("link_ids", []))
            origin = route.get("origin_node_id") or route.get("from")
            destination = route.get("destination_node_id") or route.get("to")
            demand = float(route.get("demand_veh_h", route.get("demand", 0.0)) or 0.0)
            vehicle_type = self._resolve_vehicle_type(project, route)

            if demand <= 0:
                continue

            if not link_ids and origin and destination:
                link_ids = self.routing_service.find_shortest_path(
                    project.network,
                    origin,
                    destination,
                    weight=route.get("weight", "length_km"),
                )

            if not link_ids:
                warnings.append(
                    f"Route {route_id}: no link_ids and path could not be built."
                )
                continue

            route_warnings = self._validate_route_continuity(
                project.network,
                route_id,
                link_ids,
            )
            if route_warnings:
                warnings.extend(route_warnings)
                continue

            for link_id in link_ids:
                link = project.network.links[link_id]
                link.traffic_counts[vehicle_type] = (
                    link.traffic_counts.get(vehicle_type, 0.0) + demand
                )
                link.metadata["traffic_counts_source"] = "assigned_demand"

            assigned_routes += 1

        warnings.extend(self._check_boundary_balance(project, routes, demand_mode))

        return {
            "demand_model_type": demand_mode,
            "demand_unit": self._get_demand_unit(project),
            "available_routes": len(routes),
            "assigned_routes": assigned_routes,
            "warnings": warnings,
        }

    def _get_demand_unit(self, project: Project) -> str:
        return project.demand_model.get("unit", "veh/h")

    def _resolve_vehicle_type(self, project: Project, route: dict[str, Any]) -> str:
        if self._get_demand_unit(project) == "pcu/h":
            return "pcu"
        return route.get("vehicle_type", "car")

    def _resolve_demand_mode(self, project: Project) -> tuple[str | None, list[str]]:
        warnings = []
        demand_model = project.demand_model or {}
        explicit_mode = demand_model.get("type")

        has_model_routes = bool(demand_model.get("routes"))
        has_route_splits = bool(demand_model.get(self.MODE_ROUTE_SPLITS))
        has_legacy_route_splits = bool(demand_model.get(self.LEGACY_ROUTE_SPLITS))
        has_network_routes = any(route.demand_veh_h > 0 for route in project.network.routes.values())

        if explicit_mode == self.LEGACY_ROUTE_SPLITS:
            warnings.append(
                "demand_model.type='turning_coefficients' is deprecated; "
                "use 'route_split_coefficients'."
            )
            explicit_mode = self.MODE_ROUTE_SPLITS

        if explicit_mode:
            if explicit_mode not in {self.MODE_ROUTES, self.MODE_NETWORK_ROUTES, self.MODE_ROUTE_SPLITS}:
                warnings.append(f"Unsupported demand_model.type '{explicit_mode}'.")
                return None, warnings

            ignored_sources = []
            if explicit_mode != self.MODE_ROUTES and has_model_routes:
                ignored_sources.append("routes")
            if explicit_mode != self.MODE_NETWORK_ROUTES and has_network_routes:
                ignored_sources.append("network_routes")
            if explicit_mode != self.MODE_ROUTE_SPLITS and (has_route_splits or has_legacy_route_splits):
                ignored_sources.append("route_split_coefficients")
            if ignored_sources:
                warnings.append(
                    f"demand_model.type='{explicit_mode}' is explicit; ignored sources: "
                    f"{', '.join(sorted(set(ignored_sources)))}."
                )
            return explicit_mode, warnings

        available_modes = []
        if has_model_routes:
            available_modes.append(self.MODE_ROUTES)
        if has_route_splits or has_legacy_route_splits:
            available_modes.append(self.MODE_ROUTE_SPLITS)
        if has_network_routes:
            available_modes.append(self.MODE_NETWORK_ROUTES)

        if not available_modes:
            return None, warnings

        if len(available_modes) > 1:
            warnings.append(
                "Ambiguous demand model: multiple demand sources are present. "
                "Set demand_model.type to one of: routes, network_routes, "
                "route_split_coefficients."
            )
            return None, warnings

        return available_modes[0], warnings

    def _prepare_assigned_counts(self, project: Project) -> None:
        for link in project.network.links.values():
            if (
                link.traffic_counts
                and link.metadata.get("traffic_counts_source") != "assigned_demand"
                and "observed_traffic_counts" not in link.metadata
            ):
                observed_counts = dict(link.traffic_counts)
                link.metadata["observed_traffic_counts"] = observed_counts
                # Kept for compatibility with earlier generated files.
                link.metadata.setdefault("source_traffic_counts", observed_counts)

            link.traffic_counts = {}
            link.results = {}

    def _collect_routes(
        self,
        project: Project,
        demand_mode: str,
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        if demand_mode == self.MODE_NETWORK_ROUTES:
            return self._collect_network_routes(project)

        if demand_mode == self.MODE_ROUTES:
            return self._collect_model_routes(project)

        return self._collect_route_split_routes(project, warnings)

    def _collect_network_routes(self, project: Project) -> list[dict[str, Any]]:
        result = []
        for route in project.network.routes.values():
            result.append(
                {
                    "id": route.id,
                    "name": route.name,
                    "link_ids": route.link_ids,
                    "origin_node_id": route.origin_node_id,
                    "destination_node_id": route.destination_node_id,
                    "demand_veh_h": route.demand_veh_h,
                    "vehicle_type": route.vehicle_type,
                    **route.metadata,
                }
            )
        return result

    def _collect_model_routes(self, project: Project) -> list[dict[str, Any]]:
        result = []
        for route in project.demand_model.get("routes", []):
            result.append(
                {
                    "id": route.get("id"),
                    "name": route.get("name", route.get("id", "")),
                    "link_ids": route.get("link_ids", route.get("links", [])),
                    "origin_node_id": route.get("origin_node_id", route.get("from")),
                    "destination_node_id": route.get("destination_node_id", route.get("to")),
                    "demand_veh_h": route.get("demand_veh_h", route.get("demand", 0.0)),
                    "vehicle_type": route.get("vehicle_type", "car"),
                    "weight": route.get("weight", "length_km"),
                    "from": route.get("from"),
                    "to": route.get("to"),
                }
            )
        return result

    def _collect_route_split_routes(
        self,
        project: Project,
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        route_splits = project.demand_model.get(self.MODE_ROUTE_SPLITS)
        if route_splits is None:
            route_splits = project.demand_model.get(self.LEGACY_ROUTE_SPLITS, [])
            if route_splits:
                warnings.append(
                    "demand_model.turning_coefficients is deprecated; "
                    "rename it to route_split_coefficients."
                )

        boundary_flows = project.demand_model.get("boundary_flows", {})
        result = []
        for split in route_splits:
            origin = split.get("origin_node_id") or split.get("from")
            coefficient = float(
                split.get("coefficient", split.get("share", split.get("turn_ratio", 0.0)))
                or 0.0
            )
            base_volume = split.get("base_volume_veh_h", boundary_flows.get(origin, 0.0))
            demand = split.get("demand_veh_h", float(base_volume or 0.0) * coefficient)

            result.append(
                {
                    "id": split.get("id"),
                    "name": split.get("name", split.get("id", "")),
                    "link_ids": split.get("link_ids", split.get("links", [])),
                    "origin_node_id": origin,
                    "destination_node_id": split.get("destination_node_id", split.get("to")),
                    "demand_veh_h": demand,
                    "vehicle_type": split.get("vehicle_type", "car"),
                    "weight": split.get("weight", "length_km"),
                    "from": split.get("from"),
                    "to": split.get("to"),
                    "coefficient": coefficient,
                }
            )

        return result

    def _validate_route_continuity(
        self,
        network: Network,
        route_id: str | None,
        link_ids: list[str],
    ) -> list[str]:
        warnings = []

        for link_id in link_ids:
            if link_id not in network.links:
                warnings.append(f"Route {route_id}: missing link {link_id}.")

        if warnings:
            return warnings

        for previous_link_id, next_link_id in zip(link_ids, link_ids[1:]):
            previous_link = network.links[previous_link_id]
            next_link = network.links[next_link_id]
            if previous_link.end_node_id != next_link.start_node_id:
                warnings.append(
                    f"Route {route_id}: discontinuity between {previous_link_id} "
                    f"({previous_link.end_node_id}) and {next_link_id} "
                    f"({next_link.start_node_id})."
                )

        return warnings

    def _check_boundary_balance(
        self,
        project: Project,
        routes: list[dict[str, Any]],
        demand_mode: str,
    ) -> list[str]:
        warnings = []
        boundary_flows = project.demand_model.get("boundary_flows", {})
        if not boundary_flows:
            return warnings

        outgoing_by_origin = defaultdict(float)
        for route in routes:
            origin = route.get("origin_node_id") or route.get("from")
            if origin:
                demand = float(route.get("demand_veh_h", route.get("demand", 0.0)) or 0.0)
                outgoing_by_origin[origin] += demand

        tolerance = float(project.demand_model.get("balance_tolerance_veh_h", 1e-6))
        for boundary_id, expected_volume in boundary_flows.items():
            assigned = outgoing_by_origin.get(boundary_id, 0.0)
            diff = assigned - float(expected_volume or 0.0)
            if abs(diff) > tolerance:
                warnings.append(
                    f"Boundary node {boundary_id}: expected {expected_volume}, "
                    f"assigned {assigned}, difference {diff}."
                )

        if demand_mode != self.MODE_ROUTE_SPLITS:
            return warnings

        coefficient_sums = defaultdict(float)
        route_splits = project.demand_model.get(
            self.MODE_ROUTE_SPLITS,
            project.demand_model.get(self.LEGACY_ROUTE_SPLITS, []),
        )
        for split in route_splits:
            origin = split.get("origin_node_id") or split.get("from")
            if origin:
                coefficient_sums[origin] += float(
                    split.get("coefficient", split.get("share", split.get("turn_ratio", 0.0)))
                    or 0.0
                )

        policy = project.demand_model.get("split_balance_policy", "warn_if_not_one")
        if policy == "allow_unassigned":
            return warnings

        for origin, coefficient_sum in coefficient_sums.items():
            if abs(coefficient_sum - 1.0) > 1e-6:
                warnings.append(
                    f"Boundary node {origin}: route split coefficient sum is "
                    f"{coefficient_sum}, expected 1.0."
                )

        return warnings
