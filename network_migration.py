from __future__ import annotations

from collections import defaultdict

from models import Movement, Network, Project, Sink, Source


def ensure_dynamic_schema(project: Project) -> list[str]:
    diagnostics: list[str] = []
    network = project.network

    _promote_observed_counts(network, diagnostics)

    _infer_sources(network, diagnostics)
    _infer_sinks(network, diagnostics)
    _infer_movements(network, diagnostics)

    _sync_source_demands_to_legacy_links(network)
    _mark_ambiguous_boundaries(network, diagnostics)
    project.metadata["migration_diagnostics"] = diagnostics
    return diagnostics


def _promote_observed_counts(network: Network, diagnostics: list[str]) -> None:
    incoming_by_node = {node_id: network.get_incoming_links(node_id) for node_id in network.nodes}

    for link in network.links.values():
        if link.observed_counts or not link.traffic_counts:
            continue
        explicit_source = _is_explicit_source_node(network.nodes.get(link.start_node_id))
        topological_source = not incoming_by_node.get(link.start_node_id)
        if explicit_source or topological_source:
            continue

        link.observed_counts = dict(link.traffic_counts)
        diagnostics.append(
            f"Link {link.id}: legacy traffic_counts preserved as observed_counts for calibration."
        )


def _infer_sources(network: Network, diagnostics: list[str]) -> None:
    existing_source_link_ids = {source.link_id for source in network.sources.values()}
    for link in network.links.values():
        if link.id in existing_source_link_ids:
            continue
        start_node = network.nodes.get(link.start_node_id)
        incoming = network.get_incoming_links(link.start_node_id)
        if not link.traffic_counts:
            continue
        explicit_source = _is_explicit_source_node(start_node)
        topological_source = not incoming
        if not explicit_source and not topological_source:
            continue

        network.add_source(
            Source(
                id=f"SRC_{link.id}",
                link_id=link.id,
                demand_by_type=dict(link.traffic_counts),
                inferred=True,
                metadata={"inferred_from": "node_type" if explicit_source else "topology"},
            )
        )
        diagnostics.append(
            f"Source inferred for link {link.id} from {'node_type' if explicit_source else 'topology'}."
        )


def _infer_sinks(network: Network, diagnostics: list[str]) -> None:
    existing_sink_link_ids = {sink.link_id for sink in network.sinks.values()}
    for link in network.links.values():
        if link.id in existing_sink_link_ids:
            continue
        end_node = network.nodes.get(link.end_node_id)
        outgoing = network.get_outgoing_links(link.end_node_id)
        explicit_sink = _is_explicit_sink_node(end_node)
        topological_sink = not outgoing
        if not explicit_sink and not topological_sink:
            continue

        network.add_sink(
            Sink(
                id=f"SNK_{link.id}",
                link_id=link.id,
                inferred=True,
                metadata={"inferred_from": "node_type" if explicit_sink else "topology"},
            )
        )
        diagnostics.append(
            f"Sink inferred for link {link.id} from {'node_type' if explicit_sink else 'topology'}."
        )


def _infer_movements(network: Network, diagnostics: list[str]) -> None:
    route_weights = _build_route_transition_weights(network)
    existing_from_link_ids = {movement.from_link_id for movement in network.movements.values()}

    for node_id in network.nodes:
        incoming_links = network.get_incoming_links(node_id)
        outgoing_links = network.get_outgoing_links(node_id)
        if not incoming_links or not outgoing_links:
            continue

        for from_link in incoming_links:
            if from_link.id in existing_from_link_ids:
                continue
            candidates = [
                to_link
                for to_link in outgoing_links
                if to_link.id != from_link.id and not _is_uturn(from_link, to_link)
            ]
            if not candidates:
                candidates = list(outgoing_links)
            if not candidates:
                continue

            from_route_weights = route_weights.get(from_link.id, {})
            weighted_candidates = {
                to_link.id: max(from_route_weights.get(to_link.id, 0.0), 0.0)
                for to_link in candidates
            }
            total_weight = sum(weighted_candidates.values())
            inferred_from = "routes" if total_weight > 0 else "topology"

            for to_link in candidates:
                ratio = (
                    weighted_candidates[to_link.id] / total_weight
                    if total_weight > 0
                    else 1.0 / len(candidates)
                )
                movement_id = f"M_{node_id}_{from_link.id}_{to_link.id}"
                network.add_movement(
                    Movement(
                        id=movement_id,
                        node_id=node_id,
                        from_link_id=from_link.id,
                        to_link_id=to_link.id,
                        split_ratio=ratio,
                        capacity_pcu_h=_movement_capacity(from_link),
                        control=_build_control(from_link),
                        inferred=True,
                        metadata={"inferred_from": inferred_from},
                    )
                )
            diagnostics.append(
                f"Movements inferred for incoming link {from_link.id} at node {node_id} using {inferred_from}."
            )


def _build_route_transition_weights(network: Network) -> dict[str, dict[str, float]]:
    weights: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for route in network.routes.values():
        for from_link_id, to_link_id in zip(route.link_ids, route.link_ids[1:]):
            if from_link_id in network.links and to_link_id in network.links:
                weights[from_link_id][to_link_id] += 1.0
    return weights


def _mark_ambiguous_boundaries(network: Network, diagnostics: list[str]) -> None:
    source_link_ids = {source.link_id for source in network.sources.values()}
    sink_link_ids = {sink.link_id for sink in network.sinks.values()}

    for link in network.links.values():
        if link.traffic_counts and link.id not in source_link_ids:
            link.metadata["requires_source_review"] = True
            diagnostics.append(
                f"Link {link.id}: has traffic_counts but is not an active source; review migration."
            )

        end_node = network.nodes.get(link.end_node_id)
        if _is_explicit_sink_node(end_node) and link.id not in sink_link_ids:
            link.metadata["requires_sink_review"] = True


def _sync_source_demands_to_legacy_links(network: Network) -> None:
    for source in network.sources.values():
        link = network.links.get(source.link_id)
        if link is None:
            continue
        if not link.traffic_counts:
            link.traffic_counts = dict(source.demand_by_type)


def _is_explicit_source_node(node) -> bool:
    if node is None:
        return False
    node_type = (node.node_type or "").lower()
    return node_type == "source" or node.metadata.get("role") == "source"


def _is_explicit_sink_node(node) -> bool:
    if node is None:
        return False
    node_type = (node.node_type or "").lower()
    return node_type == "sink" or node.metadata.get("role") == "sink"


def _is_uturn(from_link, to_link) -> bool:
    return from_link.start_node_id == to_link.end_node_id and from_link.end_node_id == to_link.start_node_id


def _build_control(from_link) -> dict:
    params = from_link.parameters or {}
    cycle_time = float(params.get("cycle_time", 0) or 0)
    green_time = float(params.get("green_time", 0) or 0)
    g_others = float(params.get("g_others", 0) or 0)
    is_roundabout = bool(params.get("is_ring_approach")) or "RING" in from_link.id.upper()

    if is_roundabout:
        return {"control_type": "roundabout", "green_ratio": 0.9}

    if cycle_time > 0 and green_time > 0:
        return {
            "control_type": "signalized",
            "cycle_time_s": int(cycle_time),
            "green_ratio": max(min(green_time / cycle_time, 1.0), 0.0),
            "min_green_s": int(green_time),
            "max_green_s": int(max(green_time, cycle_time - g_others)),
            "phases": [
                {
                    "phase_id": f"{from_link.id}_phase_1",
                    "green_for_movements": [],
                    "start_s": 0,
                    "end_s": max(min(int(green_time), int(cycle_time)), 1),
                }
            ],
        }

    return {"control_type": "uncontrolled"}


def _movement_capacity(from_link) -> float:
    params = from_link.parameters or {}
    lanes = params.get("lanes_count", params.get("lanes_total", 1))
    lanes_bus = params.get("lanes_bus", 0)
    try:
        effective_lanes = max(float(lanes) - float(lanes_bus), 1.0)
    except (TypeError, ValueError):
        effective_lanes = 1.0

    if from_link.link_type == "intersection":
        return float(params.get("saturation_flow_base", 1800)) * effective_lanes
    return float(params.get("capacity_per_lane_base", 1800)) * effective_lanes
