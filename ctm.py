from __future__ import annotations

from collections import defaultdict

from models import Movement, SimulationConfig
from network_dynamic import DynamicNetwork


class CTMSimulator:
    def __init__(self, network: DynamicNetwork, config: SimulationConfig):
        self.network = network
        self.config = config
        first_link = next(iter(network.links.values()), None)
        self.dt_seconds = (
            first_link.dt_seconds
            if first_link is not None
            else max(config.min_dt_seconds, min(config.dt_seconds, config.max_dt_seconds))
        )
        self._movements_by_from, self._movements_by_to = self._build_movement_indexes()
        self._movement_groups_cache = self._build_movement_groups()
        self._dynamic_split_ratios = self._initial_split_ratios()
        self._last_split_update_s: int | None = None

    def simulate(self, horizon_seconds: int | None = None) -> dict[str, dict]:
        horizon = horizon_seconds or self.config.horizon_seconds
        steps = max(int(horizon / self.dt_seconds), 1)
        stats = {
            link_id: {
                "occupancy_sum": 0.0,
                "max_queue_pcu": 0.0,
                "outflow_total": 0.0,
                "inflow_total": 0.0,
                "peak_flow_step": 0.0,
                "travel_time_sum_sec": 0.0,
                "speed_sum_kph": 0.0,
                "queue_length_sum_m": 0.0,
                "max_queue_length_m": 0.0,
                "max_occupancy_pcu": 0.0,
            }
            for link_id in self.network.links
        }

        for step in range(steps):
            current_time_s = step * self.dt_seconds
            internal_flows = self._compute_internal_flows()
            node_flows = self._compute_node_flows(current_time_s)
            node_out_by_link, node_in_by_link = self._aggregate_node_flows(node_flows)
            sink_flows = self._compute_sink_flows()
            source_flows = self._compute_source_flows(current_time_s)

            for link_id, link in self.network.links.items():
                inflows = [0.0 for _ in link.cells]
                outflows = [0.0 for _ in link.cells]

                for cell_index, flow in internal_flows[link_id].items():
                    outflows[cell_index] += flow
                    inflows[cell_index + 1] += flow

                node_out = node_out_by_link.get(link_id, 0.0)
                node_in = node_in_by_link.get(link_id, 0.0)

                if link.cells:
                    outflows[-1] += node_out + sink_flows.get(link_id, 0.0)
                    inflows[0] += node_in + source_flows.get(link_id, 0.0)

                for cell_index, cell in enumerate(link.cells):
                    cell.occupancy_pcu = max(cell.occupancy_pcu + inflows[cell_index] - outflows[cell_index], 0.0)

                total_outflow = node_out + sink_flows.get(link_id, 0.0)
                total_inflow = node_in + source_flows.get(link_id, 0.0)
                link_metrics = link.snapshot_metrics()
                total_occupancy = link_metrics["total_occupancy"]
                queue_pcu = link_metrics["queue_pcu"]
                queue_length_m = link_metrics["queue_length_m"]
                stats[link_id]["outflow_total"] += total_outflow
                stats[link_id]["inflow_total"] += total_inflow
                stats[link_id]["occupancy_sum"] += total_occupancy
                stats[link_id]["max_queue_pcu"] = max(stats[link_id]["max_queue_pcu"], queue_pcu)
                stats[link_id]["max_occupancy_pcu"] = max(stats[link_id]["max_occupancy_pcu"], total_occupancy)
                stats[link_id]["peak_flow_step"] = max(stats[link_id]["peak_flow_step"], total_outflow)
                stats[link_id]["travel_time_sum_sec"] += link_metrics["travel_time_sec"]
                stats[link_id]["speed_sum_kph"] += link_metrics["avg_cell_speed_kph"]
                stats[link_id]["queue_length_sum_m"] += queue_length_m
                stats[link_id]["max_queue_length_m"] = max(stats[link_id]["max_queue_length_m"], queue_length_m)

        for link_id, values in stats.items():
            link = self.network.links[link_id]
            length_km = max(link.length_km, 0.001)
            avg_travel_time_sec = values["travel_time_sum_sec"] / steps
            free_flow_time_sec = link.free_flow_travel_time_sec()
            values["avg_density_pcu_km"] = values["occupancy_sum"] / steps / length_km
            values["avg_flow_pcu_h"] = values["outflow_total"] * 3600.0 / max(horizon, 1)
            values["peak_flow_pcu_h"] = values["peak_flow_step"] * 3600.0 / self.dt_seconds
            values["throughput_pcu"] = values["outflow_total"]
            values["avg_travel_time_sec"] = avg_travel_time_sec
            values["free_flow_travel_time_sec"] = free_flow_time_sec
            values["delay_sec"] = max(avg_travel_time_sec - free_flow_time_sec, 0.0)
            values["avg_speed_kph"] = (
                length_km / (avg_travel_time_sec / 3600.0)
                if avg_travel_time_sec > 0
                else link.free_flow_speed_kph
            )
            values["avg_cell_speed_kph"] = values["speed_sum_kph"] / steps
            values["avg_queue_length_m"] = values["queue_length_sum_m"] / steps
            values["max_queue_length_m"] = values["max_queue_length_m"]
            values["demand_served_ratio"] = (
                values["outflow_total"] / values["inflow_total"]
                if values["inflow_total"] > 0
                else 1.0
            )
        return stats

    def _compute_internal_flows(self) -> dict[str, dict[int, float]]:
        flows: dict[str, dict[int, float]] = defaultdict(dict)
        for link_id, link in self.network.links.items():
            if link.cell_count <= 1:
                continue
            for cell_index in range(link.cell_count - 1):
                flows[link_id][cell_index] = min(
                    link.sending_capacity(cell_index),
                    link.receiving_capacity(cell_index + 1),
                )
        return flows

    def _compute_source_flows(self, current_time_s: int) -> dict[str, float]:
        source_flows: dict[str, float] = defaultdict(float)
        for source in self.network.sources.values():
            if current_time_s < source.start_time_s:
                continue
            if source.end_time_s is not None and current_time_s >= source.end_time_s:
                continue

            link = self.network.links.get(source.link_id)
            if link is None or not link.cells:
                continue
            demand_pcu_h = sum(source.demand_by_type.values())
            demand_step = demand_pcu_h * self.dt_seconds / 3600.0
            source_flows[source.link_id] += min(demand_step, link.receiving_capacity(0))
        return source_flows

    def _compute_sink_flows(self) -> dict[str, float]:
        sink_flows: dict[str, float] = {}
        for sink in self.network.sinks.values():
            link = self.network.links.get(sink.link_id)
            if link is None or not link.cells:
                continue
            capacity_step = (
                sink.capacity_pcu_h * self.dt_seconds / 3600.0
                if sink.capacity_pcu_h is not None
                else link.capacity_step
            )
            sink_flows[sink.link_id] = min(link.sending_capacity(link.cell_count - 1), capacity_step)
        return sink_flows

    def _compute_node_flows(self, current_time_s: int) -> dict[str, float]:
        self._maybe_update_split_ratios(current_time_s)
        original_sending: dict[str, float] = {}
        remaining_receiving: dict[str, float] = {}
        movement_flows: dict[str, float] = {}
        outgoing_totals: dict[str, float] = defaultdict(float)

        for link_id, link in self.network.links.items():
            if not link.cells:
                continue
            original_sending[link_id] = link.sending_capacity(link.cell_count - 1)
            remaining_receiving[link_id] = link.receiving_capacity(0)

        for movement_id in sorted(self.network.movements):
            movement = self.network.movements[movement_id]
            if self._is_uturn(movement):
                movement_flows[movement_id] = 0.0
                continue

            from_available = original_sending.get(movement.from_link_id, 0.0)
            if from_available <= 0 or remaining_receiving.get(movement.to_link_id, 0.0) <= 0:
                movement_flows[movement_id] = 0.0
                continue

            desired = from_available * self._effective_split_ratio(movement)
            cap_step = self._movement_capacity_step(movement, current_time_s)
            flow = min(desired, cap_step)
            movement_flows[movement_id] = flow
            outgoing_totals[movement.from_link_id] += flow

        for link_id, sending in original_sending.items():
            outgoing_total = outgoing_totals.get(link_id, 0.0)
            if outgoing_total > sending > 0:
                scale = sending / outgoing_total
                for movement in self._movements_by_from.get(link_id, []):
                    if movement.id in movement_flows:
                        movement_flows[movement.id] *= scale

        incoming_totals: dict[str, float] = defaultdict(float)
        for movement_id, flow in movement_flows.items():
            incoming_totals[self.network.movements[movement_id].to_link_id] += flow

        for link_id, receiving in remaining_receiving.items():
            incoming_total = incoming_totals.get(link_id, 0.0)
            if incoming_total > receiving > 0:
                scale = receiving / incoming_total
                for movement in self._movements_by_to.get(link_id, []):
                    if movement.id in movement_flows:
                        movement_flows[movement.id] *= scale
        return movement_flows

    def _movement_capacity_step(self, movement: Movement, current_time_s: int) -> float:
        from_link = self.network.links[movement.from_link_id]
        base_capacity_h = movement.capacity_pcu_h or from_link.capacity_pcu_h
        control = movement.control or {}
        control_type = control.get("control_type", "uncontrolled")
        multiplier = 1.0

        if control_type == "signalized":
            cycle_time = int(control.get("cycle_time_s", 0) or 0)
            phases = control.get("phases", [])
            if cycle_time > 0 and phases:
                cycle_offset = current_time_s % cycle_time
                phase_open = False
                for phase in phases:
                    allowed = phase.get("green_for_movements", [])
                    if allowed and movement.id not in allowed:
                        continue
                    if int(phase.get("start_s", 0)) <= cycle_offset < int(phase.get("end_s", 0)):
                        phase_open = True
                        break
                multiplier = 1.0 if phase_open else 0.0
            else:
                multiplier = float(control.get("green_ratio", 1.0) or 0.0)
        elif control_type == "roundabout":
            multiplier = float(control.get("green_ratio", 0.9) or 0.9)
        elif control_type == "priority":
            multiplier = float(control.get("green_ratio", 0.85) or 0.85)

        return max(base_capacity_h * multiplier * self.dt_seconds / 3600.0, 0.0)

    def _aggregate_node_flows(self, movement_flows: dict[str, float]) -> tuple[dict[str, float], dict[str, float]]:
        outgoing: dict[str, float] = defaultdict(float)
        incoming: dict[str, float] = defaultdict(float)
        for movement_id, flow in movement_flows.items():
            movement = self.network.movements[movement_id]
            outgoing[movement.from_link_id] += flow
            incoming[movement.to_link_id] += flow
        return outgoing, incoming

    def _build_movement_indexes(self) -> tuple[dict[str, list[Movement]], dict[str, list[Movement]]]:
        by_from: dict[str, list[Movement]] = defaultdict(list)
        by_to: dict[str, list[Movement]] = defaultdict(list)
        for movement_id in sorted(self.network.movements):
            movement = self.network.movements[movement_id]
            by_from[movement.from_link_id].append(movement)
            by_to[movement.to_link_id].append(movement)
        return dict(by_from), dict(by_to)

    def _build_movement_groups(self) -> dict[str, list[Movement]]:
        groups: dict[str, list[Movement]] = defaultdict(list)
        for movement in self.network.movements.values():
            if not self._is_uturn(movement):
                groups[movement.from_link_id].append(movement)
        return dict(groups)

    def _initial_split_ratios(self) -> dict[str, float]:
        ratios: dict[str, float] = {}
        for movements in self._movement_groups_cache.values():
            ratios.update(self._base_split_ratios(movements))
        return ratios

    def _maybe_update_split_ratios(self, current_time_s: int) -> None:
        interval = max(int(self.config.split_update_interval_s or 0), self.dt_seconds)
        if self._last_split_update_s is not None and current_time_s - self._last_split_update_s < interval:
            return

        alpha = min(max(float(self.config.split_inertia_alpha), 0.0), 1.0)
        next_ratios: dict[str, float] = dict(self._dynamic_split_ratios)
        for movements in self._movement_groups_cache.values():
            base_ratios = self._base_split_ratios(movements)
            weighted = {}
            for movement in movements:
                to_link = self.network.links.get(movement.to_link_id)
                penalty = self._receiving_penalty(to_link) if to_link is not None else 0.0
                weighted[movement.id] = base_ratios.get(movement.id, 0.0) * penalty

            target = self._normalize_weights(weighted) or base_ratios
            smoothed = {}
            for movement in movements:
                old_ratio = self._dynamic_split_ratios.get(movement.id, base_ratios.get(movement.id, 0.0))
                smoothed[movement.id] = (1.0 - alpha) * old_ratio + alpha * target.get(movement.id, 0.0)
            next_ratios.update(self._normalize_weights(smoothed) or base_ratios)

        self._dynamic_split_ratios = next_ratios
        self._last_split_update_s = current_time_s

    def _base_split_ratios(self, movements: list[Movement]) -> dict[str, float]:
        skdf_weights = {}
        for movement in movements:
            to_link = self.network.links.get(movement.to_link_id)
            skdf_weights[movement.id] = self._observed_link_weight(to_link)

        normalized = self._normalize_weights(skdf_weights)
        if normalized:
            return normalized

        movement_weights = {
            movement.id: max(float(movement.split_ratio or 0.0), 0.0)
            for movement in movements
        }
        normalized = self._normalize_weights(movement_weights)
        if normalized:
            return normalized

        if not movements:
            return {}
        equal = 1.0 / len(movements)
        return {movement.id: equal for movement in movements}

    def _observed_link_weight(self, link) -> float:
        if link is None:
            return 0.0
        observed = link.parameters.get("observed_pcu_h")
        if observed is not None:
            return max(float(observed), 0.0)
        skdf = (link.metadata or {}).get("skdf", {})
        traffic = skdf.get("traffic") if isinstance(skdf, dict) else None
        if traffic is None:
            return 0.0
        return max(float(traffic), 0.0)

    def _receiving_penalty(self, link) -> float:
        if link is None or not link.cells or link.capacity_step <= 0:
            return 0.0
        free_flow_speed = max(link.free_flow_speed_kph, 1e-9)
        speed_ratio = min(max(link.cell_speed_kph(0) / free_flow_speed, 0.0), 1.0)
        receiving_ratio = min(max(link.receiving_capacity(0) / link.capacity_step, 0.0), 1.0)
        raw_penalty = min(speed_ratio, receiving_ratio)
        power = max(float(self.config.congestion_speed_penalty_power), 0.0)
        return raw_penalty ** power

    def _effective_split_ratio(self, movement: Movement) -> float:
        return max(self._dynamic_split_ratios.get(movement.id, movement.split_ratio), 0.0)

    def _normalize_weights(self, weights: dict[str, float]) -> dict[str, float]:
        clean = {key: max(float(value), 0.0) for key, value in weights.items()}
        total = sum(clean.values())
        if total <= 0:
            return {}
        return {key: value / total for key, value in clean.items()}

    def _is_uturn(self, movement: Movement) -> bool:
        from_link = self.network.links.get(movement.from_link_id)
        to_link = self.network.links.get(movement.to_link_id)
        if from_link is None or to_link is None:
            return False
        return from_link.start_node_id == to_link.end_node_id and from_link.end_node_id == to_link.start_node_id
