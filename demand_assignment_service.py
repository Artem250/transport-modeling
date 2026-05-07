from __future__ import annotations

from collections import defaultdict
from typing import Any

from models import Network, Project, Route
from routing_service import RoutingService


class DemandAssignmentService:
    """Assigns demand to links using one explicit demand model mode."""

    MODE_ROUTES = "routes"
    MODE_NETWORK_ROUTES = "network_routes"
    MODE_ROUTE_SPLITS = "route_split_coefficients"
    MODE_NODE_TURNS = "node_turning_ratios"
    LEGACY_ROUTE_SPLITS = "turning_coefficients"
    VALID_MODES = {MODE_ROUTES, MODE_NETWORK_ROUTES, MODE_ROUTE_SPLITS, MODE_NODE_TURNS}
    VALID_UNITS = {"veh/h", "pcu/h"}

    def __init__(self) -> None:
        self.routing_service = RoutingService()

    def assign(self, project: Project) -> dict[str, Any]:
        warnings: list[str] = []
        errors: list[str] = []
        demand_mode = self._resolve_demand_mode(project, warnings, errors)

        if demand_mode is None:
            self._clear_assignment_outputs(project)
            return self._build_report(project, None, [], [], warnings, errors)

        self._prepare_assigned_counts(project)

        if demand_mode == self.MODE_NODE_TURNS:
            node_turning_events = self._assign_node_turning_model(project, warnings, errors)
            report = self._build_report(project, demand_mode, [], [], warnings, errors)
            report["node_turning_events"] = node_turning_events
            report["assigned_links"] = len(
                [
                    link
                    for link in project.network.links.values()
                    if link.metadata.get("traffic_counts_source") == "assigned_demand"
                ]
            )
            return report

        routes = self._collect_routes(project, demand_mode, warnings, errors)
        assigned_route_reports = []

        for route in routes:
            route_report = self._assign_route(project, route, errors)
            if route_report is not None:
                assigned_route_reports.append(route_report)

        warnings.extend(
            self._check_boundary_balance(project, assigned_route_reports, demand_mode)
        )

        return self._build_report(
            project,
            demand_mode,
            routes,
            assigned_route_reports,
            warnings,
            errors,
        )

    def _build_report(
        self,
        project: Project,
        demand_mode: str | None,
        routes: list[dict[str, Any]],
        assigned_route_reports: list[dict[str, Any]],
        warnings: list[str],
        errors: list[str],
    ) -> dict[str, Any]:
        return {
            "demand_model_type": demand_mode,
            "demand_unit": self._get_demand_unit(project),
            "available_routes": len(routes),
            "assigned_routes": len(assigned_route_reports),
            "routes": assigned_route_reports,
            "warnings": warnings,
            "errors": errors,
            "success": not errors,
        }

    def _resolve_demand_mode(
        self,
        project: Project,
        warnings: list[str],
        errors: list[str],
    ) -> str | None:
        demand_model = project.demand_model or {}
        explicit_mode = demand_model.get("type")

        unit = self._get_demand_unit(project)
        if unit not in self.VALID_UNITS:
            errors.append(
                f"Unsupported demand_model.unit '{unit}'. Use one of: veh/h, pcu/h."
            )
            return None

        has_model_routes = bool(demand_model.get("routes"))
        has_route_splits = bool(demand_model.get(self.MODE_ROUTE_SPLITS))
        has_legacy_route_splits = bool(demand_model.get(self.LEGACY_ROUTE_SPLITS))
        has_node_turns = bool(demand_model.get(self.MODE_NODE_TURNS))
        has_network_routes = any(
            self._get_route_demand_value(
                {
                    "demand_veh_h": route.demand_veh_h,
                    "demand_value": route.demand_value,
                }
            )
            > 0
            for route in project.network.routes.values()
        )

        if explicit_mode == self.LEGACY_ROUTE_SPLITS:
            warnings.append(
                "demand_model.type='turning_coefficients' is deprecated; "
                "use 'route_split_coefficients'."
            )
            explicit_mode = self.MODE_ROUTE_SPLITS

        if explicit_mode:
            if explicit_mode not in self.VALID_MODES:
                errors.append(f"Unsupported demand_model.type '{explicit_mode}'.")
                return None

            ignored_sources = []
            if explicit_mode != self.MODE_ROUTES and has_model_routes:
                ignored_sources.append("routes")
            if explicit_mode != self.MODE_NETWORK_ROUTES and has_network_routes:
                ignored_sources.append("network_routes")
            if explicit_mode != self.MODE_ROUTE_SPLITS and (
                has_route_splits or has_legacy_route_splits
            ):
                ignored_sources.append("route_split_coefficients")
            if explicit_mode != self.MODE_NODE_TURNS and has_node_turns:
                ignored_sources.append("node_turning_ratios")
            if ignored_sources:
                warnings.append(
                    f"demand_model.type='{explicit_mode}' is explicit; ignored sources: "
                    f"{', '.join(sorted(set(ignored_sources)))}."
                )
            return explicit_mode

        available_modes = []
        if has_model_routes:
            available_modes.append(self.MODE_ROUTES)
        if has_route_splits or has_legacy_route_splits:
            available_modes.append(self.MODE_ROUTE_SPLITS)
        if has_node_turns:
            available_modes.append(self.MODE_NODE_TURNS)
        if has_network_routes:
            available_modes.append(self.MODE_NETWORK_ROUTES)

        if not available_modes:
            return None

        if len(available_modes) > 1:
            errors.append(
                "Ambiguous demand model: multiple demand sources are present. "
                "Set demand_model.type to one of: routes, network_routes, "
                "route_split_coefficients, node_turning_ratios."
            )
            return None

        return available_modes[0]

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

    def _clear_assignment_outputs(self, project: Project) -> None:
        for link in project.network.links.values():
            if link.metadata.get("traffic_counts_source") == "assigned_demand":
                link.traffic_counts = {}
            link.results = {}

    def _collect_routes(
        self,
        project: Project,
        demand_mode: str,
        warnings: list[str],
        errors: list[str],
    ) -> list[dict[str, Any]]:
        if demand_mode == self.MODE_NETWORK_ROUTES:
            return self._collect_network_routes(project)

        if demand_mode == self.MODE_ROUTES:
            return self._collect_model_routes(project)

        return self._collect_route_split_routes(project, warnings, errors)

    def _assign_node_turning_model(
        self,
        project: Project,
        warnings: list[str],
        errors: list[str],
    ) -> list[dict[str, Any]]:
        demand_model = project.demand_model
        boundary_flows = demand_model.get("boundary_flows", {})
        turning_ratios = demand_model.get(self.MODE_NODE_TURNS, [])
        default_vehicle_type = demand_model.get("vehicle_type", "car")
        vehicle_type = "pcu" if self._get_demand_unit(project) == "pcu/h" else default_vehicle_type
        max_steps = int(demand_model.get("max_propagation_steps", 10000))

        turns_by_incoming_link = defaultdict(list)
        for turn in turning_ratios:
            share = float(turn.get("share", turn.get("coefficient", 0.0)) or 0.0)
            if share < 0:
                errors.append(f"Node turn {turn.get('id')}: share cannot be negative.")
                continue
            turns_by_incoming_link[turn.get("from_link_id", turn.get("from_link"))].append(
                {
                    "id": turn.get("id"),
                    "node_id": turn.get("node_id"),
                    "to_link_id": turn.get("to_link_id", turn.get("to_link")),
                    "share": share,
                }
            )

        queue = []
        for boundary_node_id, boundary_flow in boundary_flows.items():
            flow_value = float(boundary_flow or 0.0)
            if flow_value < 0:
                errors.append(f"Boundary flow {boundary_node_id}: volume cannot be negative.")
                continue

            entries = self._get_boundary_entries(project.network, demand_model, boundary_node_id)
            if entries is None:
                errors.append(
                    f"Boundary node {boundary_node_id}: multiple outgoing links found; "
                    "define boundary_entry_links."
                )
                continue

            entry_share_sum = sum(entry["share"] for entry in entries)
            if abs(entry_share_sum - 1.0) > 1e-6:
                errors.append(
                    f"Boundary node {boundary_node_id}: entry link share sum is "
                    f"{entry_share_sum}, expected 1.0."
                )
                continue

            for entry in entries:
                queue.append(
                    {
                        "link_id": entry["link_id"],
                        "flow": flow_value * entry["share"],
                        "origin_node_id": boundary_node_id,
                    }
                )

        events = []
        steps = 0
        while queue:
            steps += 1
            if steps > max_steps:
                errors.append(
                    f"node_turning_ratios exceeded max_propagation_steps={max_steps}; "
                    "check for loops."
                )
                break

            event = queue.pop(0)
            link_id = event["link_id"]
            flow = event["flow"]
            if flow <= 0:
                continue

            link = project.network.links.get(link_id)
            if link is None:
                errors.append(f"Node turning assignment: missing link {link_id}.")
                continue
            if link.metadata.get("disabled"):
                errors.append(f"Node turning assignment: disabled link {link_id} receives flow.")
                continue

            link.traffic_counts[vehicle_type] = link.traffic_counts.get(vehicle_type, 0.0) + flow
            link.metadata["traffic_counts_source"] = "assigned_demand"
            events.append(
                {
                    "link_id": link_id,
                    "flow": flow,
                    "unit": self._get_demand_unit(project),
                    "vehicle_type": vehicle_type,
                    "origin_node_id": event.get("origin_node_id"),
                }
            )

            outgoing_turns = turns_by_incoming_link.get(link_id, [])
            if not outgoing_turns:
                continue

            share_sum = sum(turn["share"] for turn in outgoing_turns)
            if abs(share_sum - 1.0) > 1e-6:
                warnings.append(
                    f"Node {link.end_node_id}, incoming {link_id}: turning share sum is "
                    f"{share_sum}, not 1.0."
                )

            for turn in outgoing_turns:
                to_link_id = turn["to_link_id"]
                to_link = project.network.links.get(to_link_id)
                if to_link is None:
                    errors.append(f"Node turn {turn.get('id')}: missing to_link {to_link_id}.")
                    continue
                if turn.get("node_id") and turn["node_id"] != link.end_node_id:
                    errors.append(
                        f"Node turn {turn.get('id')}: node_id {turn['node_id']} does not "
                        f"match incoming link end node {link.end_node_id}."
                    )
                    continue
                if to_link.start_node_id != link.end_node_id:
                    errors.append(
                        f"Node turn {turn.get('id')}: to_link {to_link_id} starts at "
                        f"{to_link.start_node_id}, expected {link.end_node_id}."
                    )
                    continue
                if to_link.metadata.get("disabled"):
                    errors.append(f"Node turn {turn.get('id')}: to_link {to_link_id} is disabled.")
                    continue

                queue.append(
                    {
                        "link_id": to_link_id,
                        "flow": flow * turn["share"],
                        "origin_node_id": event.get("origin_node_id"),
                    }
                )

        return events

    def _get_boundary_entries(
        self,
        network: Network,
        demand_model: dict[str, Any],
        boundary_node_id: str,
    ) -> list[dict[str, Any]] | None:
        explicit_entries = [
            entry
            for entry in demand_model.get("boundary_entry_links", [])
            if entry.get("boundary_node_id", entry.get("from")) == boundary_node_id
        ]
        if explicit_entries:
            return [
                {
                    "link_id": entry.get("link_id", entry.get("to_link_id", entry.get("to_link"))),
                    "share": float(entry.get("share", entry.get("coefficient", 1.0)) or 0.0),
                }
                for entry in explicit_entries
            ]

        outgoing_links = [
            link
            for link in network.get_outgoing_links(boundary_node_id)
            if not link.metadata.get("disabled")
        ]
        if len(outgoing_links) == 1:
            return [{"link_id": outgoing_links[0].id, "share": 1.0}]
        return None

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
                    "demand_value": route.demand_value,
                    "demand_veh_h": route.demand_veh_h,
                    "vehicle_type": route.vehicle_type,
                    "_route_object": route,
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
                    "demand_value": route.get("demand_value"),
                    "demand_veh_h": route.get("demand_veh_h", route.get("demand", 0.0)),
                    "vehicle_type": route.get("vehicle_type", "car"),
                    "weight": route.get("weight", "length_km"),
                    "from": route.get("from"),
                    "to": route.get("to"),
                    "_source_object": route,
                }
            )
        return result

    def _collect_route_split_routes(
        self,
        project: Project,
        warnings: list[str],
        errors: list[str],
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
            split_id = split.get("id")
            origin = split.get("origin_node_id") or split.get("from")
            has_demand_override = any(
                key in split for key in ("demand_value", "demand_veh_h", "demand")
            )
            if has_demand_override:
                errors.append(
                    f"Route split {split_id}: use coefficient with boundary/base flow; "
                    "do not set demand_value or demand_veh_h in route_split_coefficients."
                )
                continue

            coefficient = float(
                split.get("coefficient", split.get("share", split.get("turn_ratio", 0.0)))
                or 0.0
            )
            base_volume = split.get("base_volume_veh_h", boundary_flows.get(origin, 0.0))
            demand = float(base_volume or 0.0) * coefficient

            result.append(
                {
                    "id": split_id,
                    "name": split.get("name", split.get("id", "")),
                    "link_ids": split.get("link_ids", split.get("links", [])),
                    "origin_node_id": origin,
                    "destination_node_id": split.get("destination_node_id", split.get("to")),
                    "demand_value": demand,
                    "vehicle_type": split.get("vehicle_type", "car"),
                    "weight": split.get("weight", "length_km"),
                    "from": split.get("from"),
                    "to": split.get("to"),
                    "coefficient": coefficient,
                    "_source_object": split,
                }
            )

        return result

    def _assign_route(
        self,
        project: Project,
        route: dict[str, Any],
        errors: list[str],
    ) -> dict[str, Any] | None:
        route_id = route.get("id")
        link_ids = list(route.get("link_ids", []))
        origin = route.get("origin_node_id") or route.get("from")
        destination = route.get("destination_node_id") or route.get("to")
        demand = self._get_route_demand_value(route)
        vehicle_type = self._resolve_vehicle_type(project, route)

        if demand < 0:
            errors.append(f"Route {route_id}: demand must be non-negative.")
            return None

        if demand == 0:
            return None

        path_was_built = False
        if not link_ids and origin and destination:
            link_ids = self.routing_service.find_shortest_path(
                project.network,
                origin,
                destination,
                weight=route.get("weight", "length_km"),
            )
            path_was_built = bool(link_ids)

        if not link_ids:
            errors.append(f"Route {route_id}: no link_ids and path could not be built.")
            return None

        route_errors = self._validate_route_path(
            project.network,
            route_id,
            link_ids,
            origin,
            destination,
        )
        if route_errors:
            errors.extend(route_errors)
            return None

        self._store_assigned_path(route, link_ids, path_was_built)

        for link_id in link_ids:
            link = project.network.links[link_id]
            link.traffic_counts[vehicle_type] = (
                link.traffic_counts.get(vehicle_type, 0.0) + demand
            )
            link.metadata["traffic_counts_source"] = "assigned_demand"

        return {
            "id": route_id,
            "name": route.get("name", route_id or ""),
            "origin_node_id": origin,
            "destination_node_id": destination,
            "demand_value": demand,
            "unit": self._get_demand_unit(project),
            "vehicle_type": vehicle_type,
            "link_ids": link_ids,
            "path_was_built": path_was_built,
        }

    def _store_assigned_path(
        self,
        route: dict[str, Any],
        link_ids: list[str],
        path_was_built: bool,
    ) -> None:
        source_object = route.get("_source_object")
        if isinstance(source_object, dict):
            source_object["assigned_link_ids"] = link_ids
            if path_was_built and not source_object.get("link_ids"):
                source_object["link_ids"] = link_ids

        route_object = route.get("_route_object")
        if isinstance(route_object, Route):
            route_object.metadata["assigned_link_ids"] = link_ids
            if path_was_built and not route_object.link_ids:
                route_object.link_ids = link_ids

    def _validate_route_path(
        self,
        network: Network,
        route_id: str | None,
        link_ids: list[str],
        origin: str | None,
        destination: str | None,
    ) -> list[str]:
        errors = []

        for link_id in link_ids:
            if link_id not in network.links:
                errors.append(f"Route {route_id}: missing link {link_id}.")

        if errors:
            return errors

        disabled_links = [
            link_id for link_id in link_ids if network.links[link_id].metadata.get("disabled")
        ]
        if disabled_links:
            errors.append(
                f"Route {route_id}: route uses disabled links: {', '.join(disabled_links)}."
            )

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
                    f"({previous_link.end_node_id}) and {next_link_id} "
                    f"({next_link.start_node_id})."
                )

        return errors

    def _get_demand_unit(self, project: Project) -> str:
        return project.demand_model.get("unit", "veh/h")

    def _get_route_demand_value(self, route: dict[str, Any]) -> float:
        value = route.get("demand_value")
        if value is None:
            value = route.get("demand_veh_h", route.get("demand", 0.0))
        return float(value or 0.0)

    def _resolve_vehicle_type(self, project: Project, route: dict[str, Any]) -> str:
        if self._get_demand_unit(project) == "pcu/h":
            return "pcu"
        return route.get("vehicle_type", "car")

    def _check_boundary_balance(
        self,
        project: Project,
        assigned_route_reports: list[dict[str, Any]],
        demand_mode: str,
    ) -> list[str]:
        warnings = []
        boundary_flows = project.demand_model.get("boundary_flows", {})
        if not boundary_flows:
            return warnings

        outgoing_by_origin = defaultdict(float)
        for route in assigned_route_reports:
            origin = route.get("origin_node_id")
            if origin:
                outgoing_by_origin[origin] += float(route.get("demand_value", 0.0) or 0.0)

        tolerance = float(project.demand_model.get("balance_tolerance_veh_h", 1e-6))
        for boundary_id, expected_volume in boundary_flows.items():
            assigned = outgoing_by_origin.get(boundary_id, 0.0)
            diff = assigned - float(expected_volume or 0.0)
            if abs(diff) > tolerance:
                warnings.append(
                    f"Boundary node {boundary_id}: expected {expected_volume}, "
                    f"actually assigned {assigned}, difference {diff}."
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
