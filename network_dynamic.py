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
    def jam_density_pcu_per_km(self) -> float:
        return self.jam_density_pcu_per_km_lane * self.lanes

    @property
    def critical_density_pcu_per_km(self) -> float:
        if self.free_flow_speed_kph <= 0:
            return self.jam_density_pcu_per_km
        return min(self.capacity_pcu_h / self.free_flow_speed_kph, self.jam_density_pcu_per_km)

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
        return min(remaining_storage, self.receiving_wave_step, self.capacity_step)

    def cell_density_pcu_km(self, cell_index: int) -> float:
        cell_length_km = max(self.cell_length_m / 1000.0, 0.001)
        return self.cells[cell_index].occupancy_pcu / cell_length_km

    def cell_speed_kph(self, cell_index: int) -> float:
        density = self.cell_density_pcu_km(cell_index)
        if density <= 1e-9:
            return max(self.free_flow_speed_kph, 0.0)

        jam_density = max(self.jam_density_pcu_per_km, 1e-9)
        density = min(density, jam_density)
        free_flow = max(self.free_flow_speed_kph * density, 0.0)
        congested = max(self.wave_speed_kph * (jam_density - density), 0.0)
        flow_pcu_h = min(free_flow, congested, max(self.capacity_pcu_h, 0.0))
        if flow_pcu_h <= 1e-9:
            return 0.0
        return min(flow_pcu_h / density, max(self.free_flow_speed_kph, 0.0))

    def cell_travel_time_sec(self, cell_index: int) -> float:
        min_speed_kph = 0.1
        speed_kph = max(self.cell_speed_kph(cell_index), min_speed_kph)
        return (self.cell_length_m / 1000.0) / speed_kph * 3600.0

    def step_travel_time_sec(self) -> float:
        return sum(self.cell_travel_time_sec(index) for index in range(self.cell_count))

    def free_flow_travel_time_sec(self) -> float:
        if self.free_flow_speed_kph <= 0:
            return 0.0
        return self.length_km / self.free_flow_speed_kph * 3600.0

    def avg_cell_speed_kph(self) -> float:
        if not self.cells:
            return max(self.free_flow_speed_kph, 0.0)
        return sum(self.cell_speed_kph(index) for index in range(self.cell_count)) / self.cell_count

    def queue_length_m(self) -> float:
        congested_cells = 0
        critical_density = max(self.critical_density_pcu_per_km, 1e-9)
        for index in range(self.cell_count):
            if self.cell_density_pcu_km(index) > critical_density:
                congested_cells += 1
        return congested_cells * self.cell_length_m

    def queue_occupancy_pcu(self) -> float:
        critical_density = max(self.critical_density_pcu_per_km, 1e-9)
        return sum(
            cell.occupancy_pcu
            for index, cell in enumerate(self.cells)
            if self.cell_density_pcu_km(index) > critical_density
        )

    def snapshot_metrics(self) -> dict[str, float]:
        total_occupancy = 0.0
        queue_pcu = 0.0
        congested_cells = 0
        travel_time_sec = 0.0
        speed_sum_kph = 0.0

        cell_length_km = max(self.cell_length_m / 1000.0, 0.001)
        critical_density = max(self.critical_density_pcu_per_km, 1e-9)
        jam_density = max(self.jam_density_pcu_per_km, 1e-9)
        free_flow_speed = max(self.free_flow_speed_kph, 0.0)
        capacity = max(self.capacity_pcu_h, 0.0)

        for cell in self.cells:
            occupancy = cell.occupancy_pcu
            total_occupancy += occupancy
            density = occupancy / cell_length_km
            if density > critical_density:
                congested_cells += 1
                queue_pcu += occupancy

            if density <= 1e-9:
                speed_kph = free_flow_speed
            else:
                density = min(density, jam_density)
                free_flow = max(self.free_flow_speed_kph * density, 0.0)
                congested = max(self.wave_speed_kph * (jam_density - density), 0.0)
                flow_pcu_h = min(free_flow, congested, capacity)
                speed_kph = min(flow_pcu_h / density, free_flow_speed) if flow_pcu_h > 1e-9 else 0.0

            speed_sum_kph += speed_kph
            travel_time_sec += cell_length_km / max(speed_kph, 0.1) * 3600.0

        cell_count = max(self.cell_count, 1)
        return {
            "total_occupancy": total_occupancy,
            "queue_pcu": queue_pcu,
            "queue_length_m": congested_cells * self.cell_length_m,
            "travel_time_sec": travel_time_sec,
            "avg_cell_speed_kph": speed_sum_kph / cell_count,
        }


@dataclass
class DynamicNetwork:
    nodes: dict[str, Node]
    links: dict[str, DynamicLink]
    sources: dict[str, Source]
    sinks: dict[str, Sink]
    movements: dict[str, Movement]
    diagnostics: list[str] = field(default_factory=list)
