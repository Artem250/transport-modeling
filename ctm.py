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
            }
            for link_id in self.network.links
        }

        for step in range(steps):
            current_time_s = step * self.dt_seconds
            internal_flows = self._compute_internal_flows()
            node_flows = self._compute_node_flows(current_time_s)
            sink_flows = self._compute_sink_flows()
            source_flows = self._compute_source_flows(current_time_s)

            for link_id, link in self.network.links.items():
                inflows = [0.0 for _ in link.cells]
                outflows = [0.0 for _ in link.cells]

                for cell_index, flow in internal_flows[link_id].items():
                    outflows[cell_index] += flow
                    inflows[cell_index + 1] += flow

                node_out = sum(
                    flow
                    for movement_id, flow in node_flows.items()
                    if (
                        mov := self.network.movements[movement_id]
                    ) and (
                        getattr(mov, 'from_link_id', None) or mov.get('from_link_id') if isinstance(mov, dict) else mov.from_link_id
                    ) == link_id
                )
                node_in = sum(
                    flow
                    for movement_id, flow in node_flows.items()
                    if (
                        mov := self.network.movements[movement_id]
                    ) and (
                        getattr(mov, 'to_link_id', None) or mov.get('to_link_id') if isinstance(mov, dict) else mov.to_link_id
                    ) == link_id
                )

                if link.cells:
                    outflows[-1] += node_out + sink_flows.get(link_id, 0.0)
                    inflows[0] += node_in + source_flows.get(link_id, 0.0)

                for cell_index, cell in enumerate(link.cells):
                    cell.occupancy_pcu = max(cell.occupancy_pcu + inflows[cell_index] - outflows[cell_index], 0.0)

                total_outflow = node_out + sink_flows.get(link_id, 0.0)
                total_inflow = node_in + source_flows.get(link_id, 0.0)
                total_occupancy = link.total_occupancy()
                stats[link_id]["outflow_total"] += total_outflow
                stats[link_id]["inflow_total"] += total_inflow
                stats[link_id]["occupancy_sum"] += total_occupancy
                stats[link_id]["max_queue_pcu"] = max(stats[link_id]["max_queue_pcu"], total_occupancy)
                stats[link_id]["peak_flow_step"] = max(stats[link_id]["peak_flow_step"], total_outflow)

        for link_id, values in stats.items():
            length_km = max(self.network.links[link_id].length_km, 0.001)
            values["avg_density_pcu_km"] = values["occupancy_sum"] / steps / length_km
            values["avg_flow_pcu_h"] = values["outflow_total"] * 3600.0 / max(horizon, 1)
            values["peak_flow_pcu_h"] = values["peak_flow_step"] * 3600.0 / self.dt_seconds
            values["throughput_pcu"] = values["outflow_total"]
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
            link_id = getattr(sink, 'link_id', None) or (sink.get('link_id') if isinstance(sink, dict) else sink.link_id)
            link = self.network.links.get(link_id)
            if link is None or not link.cells:
                continue
            capacity_pcu_h = getattr(sink, 'capacity_pcu_h', None) or (sink.get('capacity_pcu_h') if isinstance(sink, dict) else sink.capacity_pcu_h)
            capacity_step = (
                capacity_pcu_h * self.dt_seconds / 3600.0
                if capacity_pcu_h is not None
                else link.capacity_step
            )
            sink_flows[link_id] = min(link.sending_capacity(link.cell_count - 1), capacity_step)
        return sink_flows

    def _compute_node_flows(self, current_time_s: int) -> dict[str, float]:
        original_sending: dict[str, float] = {}
        remaining_sending: dict[str, float] = {}
        remaining_receiving: dict[str, float] = {}
        movement_flows: dict[str, float] = {}

        for link_id, link in self.network.links.items():
            if not link.cells:
                continue
            original_sending[link_id] = link.sending_capacity(link.cell_count - 1)
            remaining_sending[link_id] = original_sending[link_id]
            remaining_receiving[link_id] = link.receiving_capacity(0)

        for movement_id in sorted(self.network.movements):
            movement = self.network.movements[movement_id]
            from_link_id = getattr(movement, 'from_link_id', None) or (movement.get('from_link_id') if isinstance(movement, dict) else movement.from_link_id)
            to_link_id = getattr(movement, 'to_link_id', None) or (movement.get('to_link_id') if isinstance(movement, dict) else movement.to_link_id)

            from_available = remaining_sending.get(from_link_id, 0.0)
            to_available = remaining_receiving.get(to_link_id, 0.0)
            if from_available <= 0 or to_available <= 0:
                movement_flows[movement_id] = 0.0
                continue

            split_ratio = getattr(movement, 'split_ratio', None) or (movement.get('split_ratio') if isinstance(movement, dict) else movement.split_ratio)
            desired = original_sending.get(from_link_id, 0.0) * max(split_ratio, 0.0)
            cap_step = self._movement_capacity_step(movement, current_time_s)
            actual = min(desired, from_available, to_available, cap_step)
            remaining_sending[from_link_id] = max(from_available - actual, 0.0)
            remaining_receiving[to_link_id] = max(to_available - actual, 0.0)
            movement_flows[movement_id] = actual
        return movement_flows

    def _movement_capacity_step(self, movement: Movement, current_time_s: int) -> float:
        from_link_id = getattr(movement, 'from_link_id', None) or (movement.get('from_link_id') if isinstance(movement, dict) else movement.from_link_id)
        from_link = self.network.links[from_link_id]

        capacity_pcu_h = getattr(movement, 'capacity_pcu_h', None) or (movement.get('capacity_pcu_h') if isinstance(movement, dict) else movement.capacity_pcu_h)
        base_capacity_h = capacity_pcu_h or from_link.capacity_pcu_h

        control = getattr(movement, 'control', None) or (movement.get('control') if isinstance(movement, dict) else movement.control) or {}
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
                    mov_id = getattr(movement, 'id', None) or (movement.get('id') if isinstance(movement, dict) else movement.id)
                    if allowed and mov_id not in allowed:
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