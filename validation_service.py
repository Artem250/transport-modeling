from __future__ import annotations

from collections import defaultdict
from typing import Any

from models import Network, Project


class ValidationService:
    VALID_DEMAND_TYPES = {
        "routes",
        "network_routes",
        "route_split_coefficients",
        "node_turning_ratios",
    }
    VALID_DEMAND_UNITS = {"veh/h", "pcu/h"}

    def validate_project(self, project: Project) -> list[str]:
        errors = []
        network = project.network
        has_demand_model = self._has_demand_model(project)

        for link in network.links.values():
            if link.start_node_id not in network.nodes:
                errors.append(
                    f"Link {link.id}: missing start node {link.start_node_id}."
                )
            if link.end_node_id not in network.nodes:
                errors.append(f"Link {link.id}: missing end node {link.end_node_id}.")
            if link.length_km < 0:
                errors.append(f"Link {link.id}: length cannot be negative.")
            if not link.traffic_counts and not has_demand_model:
                errors.append(f"Link {link.id}: traffic counts are not set.")

        for route in network.routes.values():
            errors.extend(
                self._validate_route_path(
                    network,
                    route.id,
                    route.link_ids,
                    route.origin_node_id,
                    route.destination_node_id,
                )
            )

        errors.extend(self._validate_demand_model(project))
        return errors

    def _has_demand_model(self, project: Project) -> bool:
        demand_model = project.demand_model or {}
        return (
            bool(demand_model.get("routes"))
            or bool(demand_model.get("route_split_coefficients"))
            or bool(demand_model.get("node_turning_ratios"))
            or bool(demand_model.get("turning_coefficients"))
            or any((route.demand_value or route.demand_veh_h) > 0 for route in project.network.routes.values())
        )

    def _validate_demand_model(self, project: Project) -> list[str]:
        errors = []
        demand_model = project.demand_model or {}
        if not demand_model and not any(
            (route.demand_value or route.demand_veh_h) > 0 for route in project.network.routes.values()
        ):
            return errors

        demand_type = demand_model.get("type")
        if demand_type == "turning_coefficients":
            demand_type = "route_split_coefficients"
        if demand_type and demand_type not in self.VALID_DEMAND_TYPES:
            errors.append(
                f"demand_model.type '{demand_type}' is unsupported. "
                "Use routes, network_routes, route_split_coefficients, "
                "or node_turning_ratios."
            )

        unit = demand_model.get("unit", "veh/h")
        if unit not in self.VALID_DEMAND_UNITS:
            errors.append(f"demand_model.unit '{unit}' is unsupported.")

        sources = []
        if demand_model.get("routes"):
            sources.append("routes")
        if demand_model.get("route_split_coefficients") or demand_model.get("turning_coefficients"):
            sources.append("route_split_coefficients")
        if demand_model.get("node_turning_ratios"):
            sources.append("node_turning_ratios")
        if any((route.demand_value or route.demand_veh_h) > 0 for route in project.network.routes.values()):
            sources.append("network_routes")
        if not demand_type and len(sources) > 1:
            errors.append(
                "demand_model.type is required when multiple demand sources are present."
            )

        for boundary_id, volume in demand_model.get("boundary_flows", {}).items():
            if float(volume or 0.0) < 0:
                errors.append(f"Boundary flow {boundary_id}: volume cannot be negative.")

        errors.extend(self._validate_demand_routes(project.network, demand_model.get("routes", [])))
        errors.extend(self._validate_route_splits(project.network, demand_model))
        errors.extend(self._validate_node_turning_ratios(project.network, demand_model))

        return errors

    def _validate_demand_routes(
        self,
        network: Network,
        routes: list[dict[str, Any]],
    ) -> list[str]:
        errors = []
        for route in routes:
            route_id = route.get("id")
            demand_value = self._get_demand_value(route)
            if demand_value < 0:
                errors.append(f"Route {route_id}: demand cannot be negative.")
            errors.extend(
                self._validate_route_path(
                    network,
                    route_id,
                    route.get("link_ids", route.get("links", [])),
                    route.get("origin_node_id", route.get("from")),
                    route.get("destination_node_id", route.get("to")),
                )
            )
        return errors

    def _validate_route_splits(
        self,
        network: Network,
        demand_model: dict[str, Any],
    ) -> list[str]:
        errors = []
        route_splits = demand_model.get(
            "route_split_coefficients",
            demand_model.get("turning_coefficients", []),
        )
        coefficient_sums = defaultdict(float)

        for split in route_splits:
            split_id = split.get("id")
            origin = split.get("origin_node_id") or split.get("from")
            has_demand_override = any(
                key in split for key in ("demand_value", "demand_veh_h", "demand")
            )
            if has_demand_override:
                errors.append(
                    f"Route split {split_id}: coefficient-based split cannot also "
                    "define demand_value or demand_veh_h."
                )

            coefficient = float(
                split.get("coefficient", split.get("share", split.get("turn_ratio", 0.0)))
                or 0.0
            )
            if coefficient < 0:
                errors.append(f"Route split {split_id}: coefficient cannot be negative.")
            if origin:
                coefficient_sums[origin] += coefficient

            errors.extend(
                self._validate_route_path(
                    network,
                    split_id,
                    split.get("link_ids", split.get("links", [])),
                    origin,
                    split.get("destination_node_id", split.get("to")),
                )
            )

        if demand_model.get("split_balance_policy", "warn_if_not_one") != "allow_unassigned":
            for origin, coefficient_sum in coefficient_sums.items():
                if abs(coefficient_sum - 1.0) > 1e-6:
                    errors.append(
                        f"Boundary node {origin}: route split coefficient sum is "
                        f"{coefficient_sum}, expected 1.0."
                    )

        return errors

    def _validate_route_path(
        self,
        network: Network,
        route_id: str | None,
        link_ids: list[str],
        origin: str | None,
        destination: str | None,
    ) -> list[str]:
        errors = []
        if not link_ids:
            return errors

        for link_id in link_ids:
            if link_id not in network.links:
                errors.append(f"Route {route_id}: missing link {link_id}.")
        if errors:
            return errors

        for link_id in link_ids:
            if network.links[link_id].metadata.get("disabled"):
                errors.append(f"Route {route_id}: uses disabled link {link_id}.")

        first_link = network.links[link_ids[0]]
        last_link = network.links[link_ids[-1]]
        if origin and first_link.start_node_id != origin:
            errors.append(
                f"Route {route_id}: first link {link_ids[0]} starts at "
                f"{first_link.start_node_id}, expected origin {origin}."
            )
        if destination and last_link.end_node_id != destination:
            errors.append(
                f"Route {route_id}: last link {link_ids[-1]} ends at "
                f"{last_link.end_node_id}, expected destination {destination}."
            )

        for previous_link_id, next_link_id in zip(link_ids, link_ids[1:]):
            previous_link = network.links[previous_link_id]
            next_link = network.links[next_link_id]
            if previous_link.end_node_id != next_link.start_node_id:
                errors.append(
                    f"Route {route_id}: discontinuity between {previous_link_id} "
                    f"and {next_link_id}."
                )

        return errors

    def _validate_node_turning_ratios(
        self,
        network: Network,
        demand_model: dict[str, Any],
    ) -> list[str]:
        errors = []
        if demand_model.get("type") != "node_turning_ratios" and not demand_model.get("node_turning_ratios"):
            return errors

        for boundary_id, volume in demand_model.get("boundary_flows", {}).items():
            if boundary_id not in network.nodes:
                errors.append(f"Boundary node {boundary_id}: node is missing.")
            if float(volume or 0.0) < 0:
                errors.append(f"Boundary node {boundary_id}: flow cannot be negative.")

        for entry in demand_model.get("boundary_entry_links", []):
            boundary_id = entry.get("boundary_node_id", entry.get("from"))
            link_id = entry.get("link_id", entry.get("to_link_id", entry.get("to_link")))
            if link_id not in network.links:
                errors.append(f"Boundary entry {boundary_id}: missing link {link_id}.")
                continue
            if boundary_id and network.links[link_id].start_node_id != boundary_id:
                errors.append(
                    f"Boundary entry {boundary_id}: link {link_id} starts at "
                    f"{network.links[link_id].start_node_id}."
                )

        for turn in demand_model.get("node_turning_ratios", []):
            turn_id = turn.get("id")
            node_id = turn.get("node_id")
            from_link_id = turn.get("from_link_id", turn.get("from_link"))
            to_link_id = turn.get("to_link_id", turn.get("to_link"))
            share = float(turn.get("share", turn.get("coefficient", 0.0)) or 0.0)
            if share < 0:
                errors.append(f"Node turn {turn_id}: share cannot be negative.")
            if from_link_id not in network.links:
                errors.append(f"Node turn {turn_id}: missing from_link {from_link_id}.")
                continue
            if to_link_id not in network.links:
                errors.append(f"Node turn {turn_id}: missing to_link {to_link_id}.")
                continue

            from_link = network.links[from_link_id]
            to_link = network.links[to_link_id]
            if node_id and node_id != from_link.end_node_id:
                errors.append(
                    f"Node turn {turn_id}: node_id {node_id} does not match "
                    f"from_link end node {from_link.end_node_id}."
                )
            if to_link.start_node_id != from_link.end_node_id:
                errors.append(
                    f"Node turn {turn_id}: to_link {to_link_id} starts at "
                    f"{to_link.start_node_id}, expected {from_link.end_node_id}."
                )
            if from_link.metadata.get("disabled"):
                errors.append(f"Node turn {turn_id}: from_link {from_link_id} is disabled.")
            if to_link.metadata.get("disabled"):
                errors.append(f"Node turn {turn_id}: to_link {to_link_id} is disabled.")

        return errors

    def _get_demand_value(self, route: dict[str, Any]) -> float:
        value = route.get("demand_value")
        if value is None:
            value = route.get("demand_veh_h", route.get("demand", 0.0))
        return float(value or 0.0)
