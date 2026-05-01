from __future__ import annotations

from dataclasses import dataclass, field

from models import Movement, Node, Sink, Source


@dataclass
class Cell:
    occupancy_pcu: float = 0.0


@dataclass
class DynamicLink:
    id: str
    name: str
    start_node_id: str
    end_node_id: str
    link_type: str
    length_m: float
    lanes: float
    dt_seconds: int
    free_flow_speed_kph: float
    wave_speed_kph: float
    jam_density_pcu_per_km_lane: float
    capacity_pcu_h: float
    cell_length_m: float
    parameters: dict
    metadata: dict
    cells: list[Cell] = field(default_factory=list)

    @property
    def length_km(self) -> float:
        return self.length_m / 1000.0

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def cell_storage_pcu(self) -> float:
        return self.jam_density_pcu_per_km_lane * (self.cell_length_m / 1000.0) * self.lanes

    @property
    def capacity_step(self) -> float:
        return self.capacity_pcu_h * self.dt_seconds / 3600.0

    @property
    def receiving_wave_step(self) -> float:
        return (
            self.jam_density_pcu_per_km_lane
            * self.wave_speed_kph
            * self.lanes
            * self.dt_seconds
            / 3600.0
        )

    def total_occupancy(self) -> float:
        return sum(cell.occupancy_pcu for cell in self.cells)

    def sending_capacity(self, cell_index: int) -> float:
        return min(self.cells[cell_index].occupancy_pcu, self.capacity_step)

    def receiving_capacity(self, cell_index: int) -> float:
        remaining_storage = max(self.cell_storage_pcu - self.cells[cell_index].occupancy_pcu, 0.0)
        return min(remaining_storage, self.receiving_wave_step)


@dataclass
class DynamicNetwork:
    nodes: dict[str, Node]
    links: dict[str, DynamicLink]
    sources: dict[str, Source]
    sinks: dict[str, Sink]
    movements: dict[str, Movement]
    diagnostics: list[str] = field(default_factory=list)
