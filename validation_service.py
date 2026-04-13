from __future__ import annotations

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
            if not link.traffic_counts:
                errors.append(f"Связь {link.id}: не задана интенсивность движения.")

        for route in network.routes.values():
            for link_id in route.link_ids:
                if link_id not in network.links:
                    errors.append(f"Маршрут {route.id}: отсутствует связь {link_id}.")

        return errors
