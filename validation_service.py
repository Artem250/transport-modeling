from __future__ import annotations

from collections import defaultdict
from typing import Any

from demand_model_utils import (
    FORBIDDEN_SPLIT_DEMAND_KEYS,
    VALID_DEMAND_TYPES,
    VALID_DEMAND_UNITS,
    as_float,
    validate_route_path,
)
from models import Network, Project


class ValidationService:
    BALANCE_POLICY_ALLOW_UNASSIGNED = "allow_unassigned"
    COEFFICIENT_TOLERANCE = 1e-5

    def validate_project(self, project: Project) -> list[str]:
        errors: list[str] = []
        network = project.network
        has_demand_model = bool(project.demand_model)

        for link in network.links.values():
            if link.start_node_id not in network.nodes:
                errors.append(f"Link {link.id}: missing start node {link.start_node_id}.")
            if link.end_node_id not in network.nodes:
                errors.append(f"Link {link.id}: missing end node {link.end_node_id}.")
            if link.length_km < 0:
                errors.append(f"Link {link.id}: length cannot be negative.")
            if not link.traffic_counts and not has_demand_model:
                errors.append(f"Link {link.id}: traffic counts are not set.")

        for route in network.routes.values():
            errors.extend(
                validate_route_path(
                    network,
                    f"Network route {route.id}",
                    route.link_ids,
                    route.origin_node_id,
                    route.destination_node_id,
                    require_links=False,
                )
            )

        if has_demand_model:
            errors.extend(self._validate_demand_model(project))
        return errors

    def _validate_demand_model(self, project: Project) -> list[str]:
        errors: list[str] = []
        demand_model = project.demand_model or {}
        demand_type = demand_model.get("type")
        unit = demand_model.get("unit", "veh/h")

        if demand_type not in VALID_DEMAND_TYPES:
            errors.append(
                "demand_model.type must be 'routes' or 'route_split_coefficients'."
            )
        if unit not in VALID_DEMAND_UNITS:
            errors.append("demand_model.unit must be 'veh/h' or 'pcu/h'.")

        errors.extend(self._validate_boundary_flows(project.network, demand_model))
        if demand_type == "routes":
            errors.extend(self._validate_routes(project.network, demand_model.get("routes", [])))
        if demand_type == "route_split_coefficients":
            errors.extend(self._validate_route_splits(project.network, demand_model))
        return errors

    def _validate_boundary_flows(
        self,
        network: Network,
        demand_model: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        boundary_flows = demand_model.get("boundary_flows", {})
        if not isinstance(boundary_flows, dict):
            return ["demand_model.boundary_flows must be an object."]

        for boundary_id, raw_volume in boundary_flows.items():
            label = f"Boundary flow {boundary_id}"
            volume = as_float(raw_volume, label, errors)
            if boundary_id not in network.nodes:
                errors.append(f"{label}: node is missing.")
            if volume is not None and volume < 0:
                errors.append(f"{label}: volume cannot be negative.")
        return errors

    def _validate_routes(
        self,
        network: Network,
        routes: list[dict[str, Any]],
    ) -> list[str]:
        errors: list[str] = []
        if not isinstance(routes, list) or not routes:
            return ["demand_model.routes must contain at least one route."]

        for index, route in enumerate(routes, start=1):
            route_id = route.get("id") or f"route_{index}"
            label = f"Route {route_id}"
            self._validate_nonnegative_required(route.get("demand_value"), f"{label} demand_value", errors)
            errors.extend(
                validate_route_path(
                    network,
                    label,
                    list(route.get("link_ids") or []),
                    route.get("origin_node_id") or route.get("from"),
                    route.get("destination_node_id") or route.get("to"),
                    require_links=True,
                )
            )
        return errors

    def _validate_route_splits(
        self,
        network: Network,
        demand_model: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        boundary_flows = demand_model.get("boundary_flows", {})
        route_splits = demand_model.get("route_split_coefficients", [])
        coefficient_sums: dict[str, float] = defaultdict(float)
        policy = demand_model.get("split_balance_policy")

        if not isinstance(boundary_flows, dict):
            return ["demand_model.boundary_flows must be an object."]
        if not isinstance(route_splits, list) or not route_splits:
            return ["demand_model.route_split_coefficients must contain at least one split."]

        for index, split in enumerate(route_splits, start=1):
            split_id = split.get("id") or f"split_{index}"
            label = f"Route split {split_id}"
            origin = split.get("origin_node_id") or split.get("from")
            destination = split.get("destination_node_id") or split.get("to")

            forbidden_keys = sorted(FORBIDDEN_SPLIT_DEMAND_KEYS.intersection(split))
            if forbidden_keys:
                errors.append(
                    f"{label}: demand is calculated from boundary_flows and coefficient; "
                    f"remove {', '.join(forbidden_keys)}."
                )

            if not origin:
                errors.append(f"{label}: from is required.")
            elif origin not in network.nodes:
                errors.append(f"{label}: from node {origin} is missing.")
            elif origin not in boundary_flows:
                errors.append(f"{label}: boundary flow for {origin} is missing.")

            if destination and destination not in network.nodes:
                errors.append(f"{label}: to node {destination} is missing.")

            coefficient = self._validate_nonnegative_required(
                split.get("coefficient"), f"{label} coefficient", errors
            )
            if coefficient is not None:
                if coefficient > 1:
                    errors.append(f"{label}: coefficient cannot be greater than 1.")
                if origin:
                    coefficient_sums[origin] += coefficient

            errors.extend(
                validate_route_path(
                    network,
                    label,
                    list(split.get("link_ids") or []),
                    origin,
                    destination,
                    require_links=True,
                )
            )

        for origin in boundary_flows:
            coefficient_sum = coefficient_sums.get(origin, 0.0)
            if coefficient_sum > 1.0 + self.COEFFICIENT_TOLERANCE:
                errors.append(
                    f"Boundary node {origin}: route split coefficient sum "
                    f"{coefficient_sum} is greater than 1."
                )
            elif (
                coefficient_sum < 1.0 - self.COEFFICIENT_TOLERANCE
                and policy != self.BALANCE_POLICY_ALLOW_UNASSIGNED
            ):
                errors.append(
                    f"Boundary node {origin}: route split coefficient sum "
                    f"{coefficient_sum} is less than 1."
                )
        return errors

    def _validate_nonnegative_required(
        self,
        value: Any,
        label: str,
        errors: list[str],
    ) -> float | None:
        if value is None:
            errors.append(f"{label}: is required.")
            return None
        number = as_float(value, label, errors)
        if number is None:
            return None
        if number < 0:
            errors.append(f"{label}: cannot be negative.")
            return None
        return number
