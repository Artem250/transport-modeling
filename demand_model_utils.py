from __future__ import annotations

from typing import Any

from models import Network


VALID_DEMAND_TYPES = {"routes", "route_split_coefficients"}
VALID_DEMAND_UNITS = {"veh/h", "pcu/h"}
FORBIDDEN_SPLIT_DEMAND_KEYS = {"demand_value", "demand_veh_h", "demand"}


def validate_route_path(
    network: Network,
    label: str,
    link_ids: list[str],
    origin_node_id: str | None = None,
    destination_node_id: str | None = None,
    require_links: bool = True,
) -> list[str]:
    errors: list[str] = []

    if not link_ids:
        if require_links:
            errors.append(f"{label}: link_ids are required.")
        return errors

    for link_id in link_ids:
        if link_id not in network.links:
            errors.append(f"{label}: missing link {link_id}.")
    if errors:
        return errors

    disabled_links = [
        link_id for link_id in link_ids if network.links[link_id].metadata.get("disabled")
    ]
    if disabled_links:
        errors.append(
            f"{label}: route uses disabled link(s): {', '.join(disabled_links)}."
        )

    first_link = network.links[link_ids[0]]
    last_link = network.links[link_ids[-1]]
    if origin_node_id and first_link.start_node_id != origin_node_id:
        errors.append(
            f"{label}: first link {link_ids[0]} starts at "
            f"{first_link.start_node_id}, expected origin {origin_node_id}."
        )
    if destination_node_id and last_link.end_node_id != destination_node_id:
        errors.append(
            f"{label}: last link {link_ids[-1]} ends at "
            f"{last_link.end_node_id}, expected destination {destination_node_id}."
        )

    for previous_link_id, next_link_id in zip(link_ids, link_ids[1:]):
        previous_link = network.links[previous_link_id]
        next_link = network.links[next_link_id]
        if previous_link.end_node_id != next_link.start_node_id:
            errors.append(
                f"{label}: disconnected link_ids between {previous_link_id} "
                f"({previous_link.end_node_id}) and {next_link_id} "
                f"({next_link.start_node_id})."
            )

    return errors


def as_float(value: Any, label: str, errors: list[str]) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{label}: must be a number.")
        return None
