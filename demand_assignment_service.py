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
from models import Project


class DemandAssignmentService:
    MODE_ROUTES = "routes"
    MODE_ROUTE_SPLITS = "route_split_coefficients"

    def assign(self, project: Project) -> dict[str, Any]:
        warnings: list[str] = []
        errors: list[str] = []
        demand_model = project.demand_model or {}
        model_type = demand_model.get("type")
        unit = demand_model.get("unit", "veh/h")

        if not demand_model:
            return self._report(None, unit, [], {}, warnings, errors)

        if model_type not in VALID_DEMAND_TYPES:
            errors.append(
                "demand_model.type must be 'routes' or 'route_split_coefficients'."
            )
        if unit not in VALID_DEMAND_UNITS:
            errors.append("demand_model.unit must be 'veh/h' or 'pcu/h'.")

        prepared_routes: list[dict[str, Any]] = []
        if not errors and model_type == self.MODE_ROUTES:
            prepared_routes = self._prepare_routes(project, unit, errors)
        if not errors and model_type == self.MODE_ROUTE_SPLITS:
            prepared_routes = self._prepare_route_splits(project, unit, warnings, errors)

        if errors:
            return self._report(model_type, unit, [], {}, warnings, errors)

        link_assignments = self._compute_link_assignments(prepared_routes)

        self._apply_assignments(project, link_assignments)
        return self._report(model_type, unit, prepared_routes, link_assignments, warnings, errors)

    def _prepare_routes(
        self,
        project: Project,
        unit: str,
        errors: list[str],
    ) -> list[dict[str, Any]]:
        prepared = []
        routes = project.demand_model.get("routes", [])
        if not isinstance(routes, list) or not routes:
            errors.append("demand_model.routes must contain at least one route.")
            return prepared

        for index, route in enumerate(routes, start=1):
            route_id = route.get("id") or f"route_{index}"
            label = f"Route {route_id}"
            demand = self._read_required_nonnegative(
                route.get("demand_value"), f"{label} demand_value", errors
            )
            link_ids = list(route.get("link_ids") or [])
            origin = route.get("origin_node_id") or route.get("from")
            destination = route.get("destination_node_id") or route.get("to")
            errors.extend(
                validate_route_path(
                    project.network,
                    label,
                    link_ids,
                    origin,
                    destination,
                    require_links=True,
                )
            )
            if demand is None:
                continue
            prepared.append(
                self._route_report(
                    route,
                    route_id,
                    origin,
                    destination,
                    demand,
                    unit,
                    link_ids,
                )
            )
        return prepared

    def _prepare_route_splits(
        self,
        project: Project,
        unit: str,
        warnings: list[str],
        errors: list[str],
    ) -> list[dict[str, Any]]:
        prepared = []
        demand_model = project.demand_model
        boundary_flows = demand_model.get("boundary_flows", {})
        route_splits = demand_model.get(self.MODE_ROUTE_SPLITS, [])

        if not isinstance(boundary_flows, dict):
            errors.append("demand_model.boundary_flows must be an object.")
            return prepared
        if not isinstance(route_splits, list) or not route_splits:
            errors.append("demand_model.route_split_coefficients must contain at least one split.")
            return prepared

        boundary_values: dict[str, float] = {}
        for boundary_id, raw_volume in boundary_flows.items():
            label = f"Boundary flow {boundary_id}"
            volume = as_float(raw_volume, label, errors)
            if volume is None:
                continue
            if boundary_id not in project.network.nodes:
                errors.append(f"{label}: node is missing.")
            if volume < 0:
                errors.append(f"{label}: volume cannot be negative.")
            boundary_values[boundary_id] = volume

        coefficient_sums: dict[str, float] = defaultdict(float)
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
            elif origin not in project.network.nodes:
                errors.append(f"{label}: from node {origin} is missing.")
            elif origin not in boundary_values:
                errors.append(f"{label}: boundary flow for {origin} is missing.")

            if destination and destination not in project.network.nodes:
                errors.append(f"{label}: to node {destination} is missing.")

            coefficient = self._read_required_nonnegative(
                split.get("coefficient"), f"{label} coefficient", errors
            )
            if coefficient is not None and coefficient > 1:
                errors.append(f"{label}: coefficient cannot be greater than 1.")
            if origin and coefficient is not None:
                coefficient_sums[origin] += coefficient

            link_ids = list(split.get("link_ids") or [])
            errors.extend(
                validate_route_path(
                    project.network,
                    label,
                    link_ids,
                    origin,
                    destination,
                    require_links=True,
                )
            )

            if origin is None or coefficient is None:
                continue
            demand = boundary_values.get(origin, 0.0) * coefficient
            prepared.append(
                self._route_report(
                    split,
                    split_id,
                    origin,
                    destination,
                    demand,
                    unit,
                    link_ids,
                    coefficient=coefficient,
                    boundary_flow=boundary_values.get(origin),
                )
            )

        self._validate_coefficient_sums(
            coefficient_sums,
            demand_model.get("split_balance_policy"),
            warnings,
            errors,
        )
        return prepared

    def _validate_coefficient_sums(
        self,
        coefficient_sums: dict[str, float],
        policy: str | None,
        warnings: list[str],
        errors: list[str],
    ) -> None:
        tolerance = 1e-9
        for origin, coefficient_sum in coefficient_sums.items():
            if coefficient_sum > 1.0 + tolerance:
                errors.append(
                    f"Boundary node {origin}: route split coefficient sum "
                    f"{coefficient_sum} is greater than 1."
                )
            elif coefficient_sum < 1.0 - tolerance:
                message = (
                    f"Boundary node {origin}: route split coefficient sum "
                    f"{coefficient_sum} is less than 1."
                )
                if policy == "allow_unassigned":
                    warnings.append(message)
                else:
                    errors.append(message)

    def _compute_link_assignments(
        self,
        routes: list[dict[str, Any]],
    ) -> dict[str, dict[str, float]]:
        assignments: dict[str, dict[str, float]] = {}
        for route in routes:
            demand = float(route["demand_value"])
            vehicle_key = route["vehicle_type"]
            for link_id in route["link_ids"]:
                link_counts = assignments.setdefault(link_id, {})
                link_counts[vehicle_key] = link_counts.get(vehicle_key, 0.0) + demand
        return assignments

    def _apply_assignments(
        self,
        project: Project,
        link_assignments: dict[str, dict[str, float]],
    ) -> None:
        for link in project.network.links.values():
            if (
                link.traffic_counts
                and link.metadata.get("traffic_counts_source") != "assigned_demand"
                and "observed_traffic_counts" not in link.metadata
            ):
                link.metadata["observed_traffic_counts"] = dict(link.traffic_counts)

            assigned_counts = link_assignments.get(link.id, {})
            link.traffic_counts = dict(assigned_counts)
            if assigned_counts:
                link.metadata["traffic_counts_source"] = "assigned_demand"
            elif link.metadata.get("traffic_counts_source") == "assigned_demand":
                link.metadata.pop("traffic_counts_source", None)
            link.results = {}

    def _route_report(
        self,
        source: dict[str, Any],
        route_id: str,
        origin: str | None,
        destination: str | None,
        demand: float,
        unit: str,
        link_ids: list[str],
        coefficient: float | None = None,
        boundary_flow: float | None = None,
    ) -> dict[str, Any]:
        report = {
            "id": route_id,
            "name": source.get("name", route_id),
            "origin_node_id": origin,
            "destination_node_id": destination,
            "demand_value": demand,
            "unit": unit,
            "vehicle_type": "pcu" if unit == "pcu/h" else source.get("vehicle_type", "car"),
            "link_ids": list(link_ids),
        }
        if coefficient is not None:
            report["coefficient"] = coefficient
        if boundary_flow is not None:
            report["boundary_flow"] = boundary_flow
        return report

    def _read_required_nonnegative(
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

    def _report(
        self,
        model_type: str | None,
        unit: str,
        routes: list[dict[str, Any]],
        link_assignments: dict[str, dict[str, float]],
        warnings: list[str],
        errors: list[str],
    ) -> dict[str, Any]:
        return {
            "success": not errors,
            "demand_model_type": model_type,
            "unit": unit,
            "assigned_routes": 0 if errors else len(routes),
            "routes": [] if errors else routes,
            "link_assignments": {} if errors else link_assignments,
            "warnings": warnings,
            "errors": errors,
        }
