from __future__ import annotations

from collections import defaultdict

from models import Project


class ValidationService:
    def validate_project(self, project: Project) -> list[str]:
        errors = []
        network = project.network

        for link in network.links.values():
            if link.start_node_id not in network.nodes:
                errors.append(f"Связь {link.id}: отсутствует начальный узел {link.start_node_id}.")
            if link.end_node_id not in network.nodes:
                errors.append(f"Связь {link.id}: отсутствует конечный узел {link.end_node_id}.")
            if link.length_km < 0:
                errors.append(f"Связь {link.id}: длина не может быть отрицательной.")

        if not network.sources:
            errors.append("В проекте отсутствуют источники спроса (sources).")
        if not network.sinks:
            errors.append("В проекте отсутствуют стоки (sinks).")

        for source in network.sources.values():
            if source.link_id not in network.links:
                errors.append(f"Source {source.id}: неизвестное звено {source.link_id}.")
            if any(value < 0 for value in source.demand_by_type.values()):
                errors.append(f"Source {source.id}: спрос не может быть отрицательным.")

        for sink in network.sinks.values():
            if sink.link_id not in network.links:
                errors.append(f"Sink {sink.id}: неизвестное звено {sink.link_id}.")
            if sink.capacity_pcu_h is not None and sink.capacity_pcu_h < 0:
                errors.append(f"Sink {sink.id}: пропускная способность не может быть отрицательной.")

        split_sums: dict[str, float] = defaultdict(float)
        for movement in network.movements.values():
            if movement.from_link_id not in network.links:
                errors.append(f"Movement {movement.id}: неизвестное входящее звено {movement.from_link_id}.")
            if movement.to_link_id not in network.links:
                errors.append(f"Movement {movement.id}: неизвестное исходящее звено {movement.to_link_id}.")
            if movement.node_id not in network.nodes:
                errors.append(f"Movement {movement.id}: неизвестный узел {movement.node_id}.")
            if movement.split_ratio < 0:
                errors.append(f"Movement {movement.id}: split_ratio не может быть отрицательным.")
            split_sums[movement.from_link_id] += movement.split_ratio
            errors.extend(self._validate_control(movement.id, movement.control))

        for from_link_id, split_sum in split_sums.items():
            if split_sum > 1.05:
                errors.append(f"Movement group for {from_link_id}: сумма split_ratio превышает 1.0 ({split_sum:.3f}).")

        for route in network.routes.values():
            for link_id in route.link_ids:
                if link_id not in network.links:
                    errors.append(f"Маршрут {route.id}: отсутствует связь {link_id}.")

        if project.simulation.dt_seconds < project.simulation.min_dt_seconds:
            errors.append("SimulationConfig: dt_seconds меньше min_dt_seconds.")
        if project.simulation.dt_seconds > project.simulation.max_dt_seconds:
            errors.append("SimulationConfig: dt_seconds больше max_dt_seconds.")

        return errors

    def _validate_control(self, movement_id: str, control: dict) -> list[str]:
        errors: list[str] = []
        if not control:
            return errors

        control_type = control.get("control_type", "uncontrolled")
        if control_type not in {"uncontrolled", "signalized", "roundabout", "priority"}:
            errors.append(f"Movement {movement_id}: неизвестный control_type {control_type}.")

        phases = control.get("phases", [])
        for phase in phases:
            start_s = int(phase.get("start_s", 0))
            end_s = int(phase.get("end_s", 0))
            if end_s <= start_s:
                errors.append(f"Movement {movement_id}: фаза {phase.get('phase_id', '?')} имеет некорректный интервал.")

        return errors
