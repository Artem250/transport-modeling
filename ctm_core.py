from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


EPS = 1e-9


class CtmConfigurationError(ValueError):
    """Raised when CTM parameters violate physical/numerical constraints."""


@dataclass(frozen=True)
class TriangularFundamentalDiagram:
    """Triangular fundamental diagram for one directed road link.

    Units:
    - free_flow_speed_kph: km/h
    - wave_speed_kph: km/h, positive magnitude of backward congestion wave
    - capacity_pcu_h: pcu/h for this directed link, not both directions together
    - jam_density_pcu_km: pcu/km for this directed link, not per lane

    The CTM implementation below uses a demand/supply form:

        sending   = min(n_i, Q * dt)
        receiving = min(Q * dt, w * (N_i - n_i) * dt / dx)

    where N_i = jam_density * dx is storage of a cell.
    """

    free_flow_speed_kph: float
    wave_speed_kph: float
    capacity_pcu_h: float
    jam_density_pcu_km: float

    @property
    def free_flow_speed_m_s(self) -> float:
        return self.free_flow_speed_kph / 3.6

    @property
    def wave_speed_m_s(self) -> float:
        return self.wave_speed_kph / 3.6

    @property
    def capacity_pcu_s(self) -> float:
        return self.capacity_pcu_h / 3600.0

    @property
    def jam_density_pcu_m(self) -> float:
        return self.jam_density_pcu_km / 1000.0

    @property
    def critical_density_pcu_km(self) -> float:
        if self.free_flow_speed_kph <= 0:
            return 0.0
        return self.capacity_pcu_h / self.free_flow_speed_kph

    def validate(self) -> None:
        if self.free_flow_speed_kph <= 0:
            raise CtmConfigurationError("free_flow_speed_kph must be positive")
        if self.wave_speed_kph <= 0:
            raise CtmConfigurationError("wave_speed_kph must be positive")
        if self.capacity_pcu_h <= 0:
            raise CtmConfigurationError("capacity_pcu_h must be positive")
        if self.jam_density_pcu_km <= 0:
            raise CtmConfigurationError("jam_density_pcu_km must be positive")
        if self.critical_density_pcu_km >= self.jam_density_pcu_km:
            raise CtmConfigurationError(
                "critical density must be below jam density; check capacity, speed, and jam_density"
            )


@dataclass
class CtmCell:
    """One CTM cell state.

    occupancy_pcu is the number of passenger-car units currently inside the cell.
    Density is derived from occupancy and cell length, not stored independently.
    """

    occupancy_pcu: float = 0.0


@dataclass
class CtmLink:
    """Directed CTM link split into homogeneous cells."""

    id: str
    length_m: float
    cell_length_m: float
    diagram: TriangularFundamentalDiagram
    cells: list[CtmCell] = field(default_factory=list)

    @classmethod
    def create_empty(
        cls,
        id: str,
        length_m: float,
        target_cell_length_m: float,
        diagram: TriangularFundamentalDiagram,
    ) -> "CtmLink":
        diagram.validate()
        if length_m <= 0:
            raise CtmConfigurationError("length_m must be positive")
        if target_cell_length_m <= 0:
            raise CtmConfigurationError("target_cell_length_m must be positive")

        cell_count = max(1, round(length_m / target_cell_length_m))
        cell_length_m = length_m / cell_count
        return cls(
            id=id,
            length_m=length_m,
            cell_length_m=cell_length_m,
            diagram=diagram,
            cells=[CtmCell() for _ in range(cell_count)],
        )

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def cell_storage_pcu(self) -> float:
        return self.diagram.jam_density_pcu_m * self.cell_length_m

    def density_pcu_km(self, cell_index: int) -> float:
        return self.cells[cell_index].occupancy_pcu / (self.cell_length_m / 1000.0)

    def total_occupancy_pcu(self) -> float:
        return sum(cell.occupancy_pcu for cell in self.cells)

    def average_density_pcu_km(self) -> float:
        return self.total_occupancy_pcu() / (self.length_m / 1000.0)

    def clamp_occupancies(self) -> None:
        storage = self.cell_storage_pcu
        for cell in self.cells:
            cell.occupancy_pcu = min(max(cell.occupancy_pcu, 0.0), storage)


def max_stable_dt_seconds(cell_length_m: float, diagram: TriangularFundamentalDiagram) -> float:
    """Return the CFL-safe maximum time step for this cell and diagram.

    For CTM with a triangular fundamental diagram, both free-flow propagation and
    backward congestion wave must not cross more than one cell per step:

        dt <= dx / max(v, w)
    """

    diagram.validate()
    if cell_length_m <= 0:
        raise CtmConfigurationError("cell_length_m must be positive")
    return cell_length_m / max(diagram.free_flow_speed_m_s, diagram.wave_speed_m_s)


def validate_cfl_or_raise(cell_length_m: float, dt_seconds: float, diagram: TriangularFundamentalDiagram) -> None:
    max_dt = max_stable_dt_seconds(cell_length_m, diagram)
    if dt_seconds - max_dt > EPS:
        raise CtmConfigurationError(
            f"CFL condition violated: dt={dt_seconds:.3f}s, max_dt={max_dt:.3f}s "
            f"for cell_length={cell_length_m:.3f}m, "
            f"v={diagram.free_flow_speed_kph:.1f}km/h, w={diagram.wave_speed_kph:.1f}km/h"
        )


def safe_dt_seconds(cell_length_m: float, requested_dt_seconds: float, diagram: TriangularFundamentalDiagram) -> float:
    """Return min(requested_dt, CFL max dt)."""

    if requested_dt_seconds <= 0:
        raise CtmConfigurationError("requested_dt_seconds must be positive")
    return min(requested_dt_seconds, max_stable_dt_seconds(cell_length_m, diagram))


def capacity_per_step_pcu(diagram: TriangularFundamentalDiagram, dt_seconds: float) -> float:
    return diagram.capacity_pcu_s * dt_seconds


def sending_pcu(cell: CtmCell, diagram: TriangularFundamentalDiagram, dt_seconds: float) -> float:
    """Demand/sending flow from a cell during one time step, in pcu/step."""

    return min(max(cell.occupancy_pcu, 0.0), capacity_per_step_pcu(diagram, dt_seconds))


def receiving_pcu(cell: CtmCell, cell_length_m: float, diagram: TriangularFundamentalDiagram, dt_seconds: float) -> float:
    """Supply/receiving flow into a cell during one time step, in pcu/step."""

    storage_pcu = diagram.jam_density_pcu_m * cell_length_m
    free_space_pcu = max(storage_pcu - cell.occupancy_pcu, 0.0)

    # Congested branch receiving bound. Since free_space is pcu and w*dt/dx is
    # dimensionless, the result is pcu/step.
    wave_receiving_pcu = free_space_pcu * diagram.wave_speed_m_s * dt_seconds / cell_length_m
    return min(capacity_per_step_pcu(diagram, dt_seconds), wave_receiving_pcu)


def internal_flows_pcu(link: CtmLink, dt_seconds: float, *, validate_cfl: bool = True) -> list[float]:
    """Compute flows from cell i to i+1 for all internal cell boundaries.

    Returns a list of length cell_count - 1, measured in pcu per time step.
    The link state is not modified by this function.
    """

    if validate_cfl:
        validate_cfl_or_raise(link.cell_length_m, dt_seconds, link.diagram)

    flows = []
    for i in range(link.cell_count - 1):
        demand = sending_pcu(link.cells[i], link.diagram, dt_seconds)
        supply = receiving_pcu(link.cells[i + 1], link.cell_length_m, link.diagram, dt_seconds)
        flows.append(min(demand, supply))
    return flows


def step_link_pcu(
    link: CtmLink,
    dt_seconds: float,
    upstream_inflow_pcu: float = 0.0,
    downstream_outflow_capacity_pcu: float | None = None,
    *,
    validate_cfl: bool = True,
) -> dict[str, float | list[float]]:
    """Advance one directed CTM link by one time step.

    Parameters:
    - upstream_inflow_pcu: external inflow into the first cell, pcu/step.
      It is still limited by first-cell receiving supply.
    - downstream_outflow_capacity_pcu: optional external capacity after the last
      cell, pcu/step. If omitted, last-cell outflow is limited only by sending.

    Returns diagnostics with actual inflow/outflow and internal flows.
    """

    if validate_cfl:
        validate_cfl_or_raise(link.cell_length_m, dt_seconds, link.diagram)

    link.clamp_occupancies()
    flows = internal_flows_pcu(link, dt_seconds, validate_cfl=False)

    first_receiving = receiving_pcu(link.cells[0], link.cell_length_m, link.diagram, dt_seconds)
    actual_upstream_inflow = min(max(upstream_inflow_pcu, 0.0), first_receiving)

    last_sending = sending_pcu(link.cells[-1], link.diagram, dt_seconds)
    if downstream_outflow_capacity_pcu is None:
        actual_downstream_outflow = last_sending
    else:
        actual_downstream_outflow = min(last_sending, max(downstream_outflow_capacity_pcu, 0.0))

    inflows = [0.0 for _ in link.cells]
    outflows = [0.0 for _ in link.cells]
    inflows[0] += actual_upstream_inflow
    outflows[-1] += actual_downstream_outflow

    for i, flow in enumerate(flows):
        outflows[i] += flow
        inflows[i + 1] += flow

    for i, cell in enumerate(link.cells):
        cell.occupancy_pcu = cell.occupancy_pcu + inflows[i] - outflows[i]

    link.clamp_occupancies()
    return {
        "actual_upstream_inflow_pcu": actual_upstream_inflow,
        "actual_downstream_outflow_pcu": actual_downstream_outflow,
        "internal_flows_pcu": flows,
        "total_occupancy_pcu": link.total_occupancy_pcu(),
        "average_density_pcu_km": link.average_density_pcu_km(),
    }


def initialize_link_uniform_density(link: CtmLink, density_pcu_km: float) -> None:
    """Set all cells to a uniform density."""

    if density_pcu_km < 0:
        raise CtmConfigurationError("density_pcu_km must be non-negative")
    occupancy = density_pcu_km * (link.cell_length_m / 1000.0)
    for cell in link.cells:
        cell.occupancy_pcu = occupancy
    link.clamp_occupancies()


def densities_pcu_km(link: CtmLink) -> list[float]:
    return [link.density_pcu_km(i) for i in range(link.cell_count)]


def occupancies_pcu(link: CtmLink) -> list[float]:
    return [cell.occupancy_pcu for cell in link.cells]


def total_vehicle_conservation_error(before: Iterable[float], after: Iterable[float], inflow: float, outflow: float) -> float:
    """Diagnostic: after_total - (before_total + inflow - outflow)."""

    return sum(after) - (sum(before) + inflow - outflow)
