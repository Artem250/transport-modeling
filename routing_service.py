from __future__ import annotations

import heapq

from models import Network


class RoutingService:
    def find_shortest_path(self, network: Network, start_node_id: str, end_node_id: str, weight: str = "length_km") -> list[str]:
        distances = {start_node_id: 0.0}
        previous_nodes = {}
        previous_links = {}
        queue = [(0.0, start_node_id)]
        visited = set()

        while queue:
            current_distance, current_node_id = heapq.heappop(queue)
            if current_node_id in visited:
                continue
            visited.add(current_node_id)

            if current_node_id == end_node_id:
                break

            for link in network.get_outgoing_links(current_node_id):
                if link.metadata.get("disabled"):
                    continue
                next_node_id = link.end_node_id
                link_weight = self._get_link_weight(link, weight)
                candidate_distance = current_distance + link_weight

                if candidate_distance < distances.get(next_node_id, float("inf")):
                    distances[next_node_id] = candidate_distance
                    previous_nodes[next_node_id] = current_node_id
                    previous_links[next_node_id] = link.id
                    heapq.heappush(queue, (candidate_distance, next_node_id))

        if end_node_id not in distances:
            return []

        path_links = []
        cursor = end_node_id
        while cursor != start_node_id:
            link_id = previous_links[cursor]
            path_links.append(link_id)
            cursor = previous_nodes[cursor]

        path_links.reverse()
        return path_links

    def _get_link_weight(self, link, weight: str) -> float:
        if weight == "travel_time_sec":
            delay = link.results.get("Delay_sec", 0.0)
            base_speed_kph = 60.0
            base_travel_time_sec = (link.length_km / base_speed_kph) * 3600 if link.length_km else 0.0
            return base_travel_time_sec + delay
        if weight == "delay_sec":
            return float(link.results.get("Delay_sec", 0.0))
        return float(getattr(link, weight, link.length_km))
