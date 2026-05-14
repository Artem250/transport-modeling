from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


AnalysisMode = Literal["dynamic", "static", "compare"]


@dataclass
class SimulationConfig:
    horizon_seconds: int = 3600
    dt_seconds: int = 1
    min_dt_seconds: int = 1
    max_dt_seconds: int = 5
    target_cell_length_m: float = 25.0
    adaptive_dt_enabled: bool = True
    free_flow_speed_kph: float = 60.0
    wave_speed_kph: float = 20.0
    jam_density_pcu_per_km_lane: float = 150.0
    capacity_per_lane_base: float = 1800.0
    split_update_interval_s: int = 30
    split_inertia_alpha: float = 0.25
    congestion_speed_penalty_power: float = 1.0
    directional_split_ratio: float = 0.5
    group_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    link_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


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
    observed_counts: dict[str, float] = field(default_factory=dict)
    coords: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    results: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Route:
    id: str
    name: str
    link_ids: list[str] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)


@dataclass
class Scenario:
    id: str
    name: str
    description: str = ""
    changes: list[dict[str, Any]] = field(default_factory=list)
    results_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class Source:
    id: str
    link_id: str
    demand_by_type: dict[str, float] = field(default_factory=dict)
    start_time_s: int = 0
    end_time_s: int | None = None
    inferred: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sink:
    id: str
    link_id: str
    capacity_pcu_h: float | None = None
    inferred: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Movement:
    id: str
    node_id: str
    from_link_id: str
    to_link_id: str
    split_ratio: float = 1.0
    capacity_pcu_h: float | None = None
    control: dict[str, Any] = field(default_factory=dict)
    inferred: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Network:
    nodes: dict[str, Node] = field(default_factory=dict)
    links: dict[str, Link] = field(default_factory=dict)
    routes: dict[str, Route] = field(default_factory=dict)
    sources: dict[str, Source] = field(default_factory=dict)
    sinks: dict[str, Sink] = field(default_factory=dict)
    movements: dict[str, Movement] = field(default_factory=dict)

    def add_node(self, node: Node) -> None:
        self.nodes[node.id] = node

    def add_link(self, link: Link) -> None:
        self.links[link.id] = link

    def add_route(self, route: Route) -> None:
        self.routes[route.id] = route

    def add_source(self, source: Source) -> None:
        self.sources[source.id] = source

    def add_sink(self, sink: Sink) -> None:
        self.sinks[sink.id] = sink

    def add_movement(self, movement: Movement) -> None:
        self.movements[movement.id] = movement

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
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    analysis_mode: AnalysisMode = "dynamic"
    metadata: dict[str, Any] = field(default_factory=dict)
