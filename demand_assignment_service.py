from __future__ import annotations

from collections import defaultdict
from typing import Any

from models import Project
from routing_service import RoutingService


class DemandAssignmentService:
    """Assigns route demand to network links."""

    def __init__(self) -> None:
        self.routing_service = RoutingService()

    def assign(self, project: Project) -> dict[str, Any]:
        self._reset_link_volumes(project)

        warnings = []
        assigned_routes = 0
        routes = self._collect_routes(project)

        for route in routes:
            link_ids = list(route.get("link_ids", []))
            origin = route.get("origin_node_id") or route.get("from")
            destination = route.get("destination_node_id") or route.get("to")
            demand = float(route.get("demand_veh_h", route.get("demand", 0.0)) or 0.0)
            vehicle_type = route.get("vehicle_type", "car")

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
                    f"Маршрут {route.get('id')} не имеет link_ids и не может быть построен."
                )
                continue

            assigned_any_link = False
            for link_id in link_ids:
                link = project.network.links.get(link_id)
                if link is None:
                    warnings.append(
                        f"Маршрут {route.get('id')} ссылается на отсутствующий link {link_id}."
                    )
                    continue

                link.traffic_counts[vehicle_type] = (
                    link.traffic_counts.get(vehicle_type, 0.0) + demand
                )
                assigned_any_link = True

            if assigned_any_link:
                assigned_routes += 1

        warnings.extend(self._check_boundary_balance(project, routes))

        return {
            "available_routes": len(routes),
            "assigned_routes": assigned_routes,
            "warnings": warnings,
        }

    def _reset_link_volumes(self, project: Project) -> None:
        for link in project.network.links.values():
            if link.traffic_counts and "source_traffic_counts" not in link.metadata:
                link.metadata["source_traffic_counts"] = dict(link.traffic_counts)
            link.traffic_counts = {}

    def _collect_routes(self, project: Project) -> list[dict[str, Any]]:
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

        boundary_flows = project.demand_model.get("boundary_flows", {})
        for movement in project.demand_model.get("turning_coefficients", []):
            origin = movement.get("origin_node_id") or movement.get("from")
            coefficient = float(
                movement.get(
                    "coefficient",
                    movement.get("share", movement.get("turn_ratio", 0.0)),
                )
                or 0.0
            )
            base_volume = movement.get("base_volume_veh_h", boundary_flows.get(origin, 0.0))
            demand = movement.get("demand_veh_h", float(base_volume or 0.0) * coefficient)

            result.append(
                {
                    "id": movement.get("id"),
                    "name": movement.get("name", movement.get("id", "")),
                    "link_ids": movement.get("link_ids", movement.get("links", [])),
                    "origin_node_id": origin,
                    "destination_node_id": movement.get("destination_node_id", movement.get("to")),
                    "demand_veh_h": demand,
                    "vehicle_type": movement.get("vehicle_type", "car"),
                    "weight": movement.get("weight", "length_km"),
                    "from": movement.get("from"),
                    "to": movement.get("to"),
                    "coefficient": coefficient,
                }
            )

        return result

    def _check_boundary_balance(self, project: Project, routes: list[dict[str, Any]]) -> list[str]:
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

        for boundary_id, expected_volume in boundary_flows.items():
            assigned = outgoing_by_origin.get(boundary_id, 0.0)
            diff = assigned - float(expected_volume or 0.0)
            if abs(diff) > 1e-6:
                warnings.append(
                    f"Граничный узел {boundary_id}: задано {expected_volume}, "
                    f"по маршрутам распределено {assigned}, разница {diff}."
                )

        coefficient_sums = defaultdict(float)
        for movement in project.demand_model.get("turning_coefficients", []):
            origin = movement.get("origin_node_id") or movement.get("from")
            if origin:
                coefficient_sums[origin] += float(
                    movement.get(
                        "coefficient",
                        movement.get("share", movement.get("turn_ratio", 0.0)),
                    )
                    or 0.0
                )

        for origin, coefficient_sum in coefficient_sums.items():
            if abs(coefficient_sum - 1.0) > 1e-6:
                warnings.append(
                    f"Граничный узел {origin}: сумма поворотных коэффициентов "
                    f"{coefficient_sum}, должна быть 1.0."
                )

        return warnings
