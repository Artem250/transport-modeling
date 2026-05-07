from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Node:
    id: str
    lon: float | None = None
    lat: float | None = None
    x: float | None = None
    y: float | None = None
    node_type: str = "intersection"
    name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Link:
    id: str
    name: str
    start_node_id: str
    end_node_id: str
    link_type: str = "straight"
    length_km: float = 0.0
    traffic_counts: dict[str, float] = field(default_factory=dict)
    coords: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Route:
    id: str
    name: str
    link_ids: list[str] = field(default_factory=list)
    origin_node_id: str | None = None
    destination_node_id: str | None = None
    demand_value: float | None = None
    vehicle_type: str = "car"
    results: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    id: str
    name: str
    description: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)
    results_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class Network:
    nodes: dict[str, Node] = field(default_factory=dict)
    links: dict[str, Link] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def add_link(self, link: Link) -> None:
        self.links[link.id] = link

    def add_route(self, route: Route) -> None:
        self.routes[route.id] = route

    def get_outgoing_links(self, node_id: str) -> list[Link]:
        return [link for link in self.links.values() if link.start_node_id == node_id]

    def get_incoming_links(self, node_id: str) -> list[Link]:
        return [link for link in self.links.values() if link.end_node_id == node_id]


@dataclass
class Project:
    project_name: str = "Unnamed Project"
    pcu_coefficients: dict[str, float] = field(default_factory=dict)
    network: Network = field(default_factory=Network)
    scenarios: list[Scenario] = field(default_factory=list)
    demand_model: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

