from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable


EPS = 1e-9


class CTMConfigurationError(ValueError):
    pass


class CTMStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class TriangularFundamentalDiagram:
    free_flow_speed: float
    backward_wave_speed: float
    capacity: float
    jam_density: float

    @classmethod
    def from_common_units(
        cls,
        *,
        free_flow_speed_kph: float,
        backward_wave_speed_kph: float,
        capacity_pcu_h: float,
        jam_density_pcu_km: float,
    ) -> "TriangularFundamentalDiagram":
        return cls(
            free_flow_speed=free_flow_speed_kph / 3.6,
            backward_wave_speed=backward_wave_speed_kph / 3.6,
            capacity=capacity_pcu_h / 3600.0,
            jam_density=jam_density_pcu_km / 1000.0,
        )

    @property
    def critical_density(self) -> float:
        return self.capacity / self.free_flow_speed

    def validate(self) -> None:
        if self.free_flow_speed <= 0:
            raise CTMConfigurationError("Скорость свободного потока должна быть положительной")
        if self.backward_wave_speed <= 0:
            raise CTMConfigurationError("Скорость обратной волны должна быть положительной")
        if self.capacity <= 0:
            raise CTMConfigurationError("Пропускная способность должна быть положительной")
        if self.jam_density <= 0:
            raise CTMConfigurationError("Плотность затора должна быть положительной")
        if self.critical_density >= self.jam_density:
            raise CTMConfigurationError(
                "Критическая плотность должна быть меньше плотности затора; "
                "проверьте скорость, пропускную способность и плотность затора"
            )


@dataclass
class Cell:
    length: float
    density: float = 0.0
    capacity_factor: float = 1.0
    speed_factor: float = 1.0

    @property
    def occupancy(self) -> float:
        return self.density * self.length

    def set_occupancy(self, occupancy: float) -> None:
        self.density = occupancy / self.length


@dataclass(frozen=True)
class Incident:
    cell_index: int
    start_time: float
    end_time: float
    capacity_factor: float = 1.0
    speed_factor: float = 1.0

    def is_active(self, time: float) -> bool:
        return self.start_time <= time < self.end_time


@dataclass
class CTMModel:
    cells: list[Cell]
    diagram: TriangularFundamentalDiagram
    dt: float
    time: float = 0.0

    upstream_demand: Callable[[float], float] = lambda _time: 0.0
    downstream_capacity: Callable[[float], float] = lambda _time: float("inf")
    incidents: list[Incident] = field(default_factory=list)

    external_queue: float = 0.0
    strict: bool = True
    validate_cfl: bool = True

    def __post_init__(self) -> None:
        self.diagram.validate()
        if not self.cells:
            raise CTMConfigurationError("CTM-модель должна содержать хотя бы одну ячейку")
        if self.dt <= 0:
            raise CTMConfigurationError("Шаг времени dt должен быть положительным")
        for cell in self.cells:
            if cell.length <= 0:
                raise CTMConfigurationError("Длина каждой CTM-ячейки должна быть положительной")
        if self.validate_cfl:
            self.validate_cfl_or_raise()

    @classmethod
    def create_uniform_link(
        cls,
        *,
        length: float,
        cell_length: float,
        diagram: TriangularFundamentalDiagram,
        dt: float,
        initial_density: float = 0.0,
        **kwargs,
    ) -> "CTMModel":
        if length <= 0:
            raise CTMConfigurationError("Длина link должна быть положительной")
        if cell_length <= 0:
            raise CTMConfigurationError("Целевая длина CTM-ячейки должна быть положительной")
        cell_count = max(1, round(length / cell_length))
        actual_cell_length = length / cell_count
        cells = [
            Cell(length=actual_cell_length, density=initial_density)
            for _ in range(cell_count)
        ]
        return cls(cells=cells, diagram=diagram, dt=dt, **kwargs)

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    def max_stable_dt(self) -> float:
        min_length = min(cell.length for cell in self.cells)
        return min_length / max(
            self.diagram.free_flow_speed,
            self.diagram.backward_wave_speed,
        )

    def validate_cfl_or_raise(self) -> None:
        max_dt = self.max_stable_dt()
        if self.dt - max_dt > EPS:
            raise CTMConfigurationError(
                f"Нарушено условие CFL: dt={self.dt:.6g} с, "
                f"максимально допустимое dt={max_dt:.6g} с. "
                "Уменьшите шаг времени или увеличьте длину ячейки."
            )

    def reset_local_factors(self) -> None:
        for cell in self.cells:
            cell.capacity_factor = 1.0
            cell.speed_factor = 1.0

    def apply_incidents(self) -> None:
        self.reset_local_factors()
        for incident in self.incidents:
            if not (0 <= incident.cell_index < self.cell_count):
                raise CTMConfigurationError(
                    f"Индекс ячейки аварии {incident.cell_index} вне диапазона CTM-ячееек"
                )
            if incident.is_active(self.time):
                cell = self.cells[incident.cell_index]
                cell.capacity_factor *= incident.capacity_factor
                cell.speed_factor *= incident.speed_factor

    def capacity_of(self, cell: Cell) -> float:
        return max(0.0, self.diagram.capacity * cell.capacity_factor)

    def demand(self, cell: Cell) -> float:
        free_flow = (
            self.diagram.free_flow_speed
            * max(0.0, cell.speed_factor)
            * max(0.0, cell.density)
        )
        return min(free_flow, self.capacity_of(cell))

    def supply(self, cell: Cell) -> float:
        available_density = max(0.0, self.diagram.jam_density - cell.density)
        congested_branch = self.diagram.backward_wave_speed * available_density
        return min(congested_branch, self.capacity_of(cell))

    def compute_boundary_flows(self) -> list[float]:
        cells = self.cells
        flows: list[float] = []

        new_external_demand = max(0.0, self.upstream_demand(self.time)) * self.dt
        self.external_queue += new_external_demand

        queued_demand_rate = self.external_queue / self.dt
        upstream_flow = min(queued_demand_rate, self.supply(cells[0]))
        flows.append(upstream_flow)

        flows.extend(self.compute_internal_flows())

        downstream_flow = min(
            self.demand(cells[-1]),
            max(0.0, self.downstream_capacity(self.time)),
        )
        flows.append(downstream_flow)

        return flows

    def compute_internal_flows(self) -> list[float]:
        flows: list[float] = []
        for boundary_index in range(1, self.cell_count):
            upstream_cell = self.cells[boundary_index - 1]
            downstream_cell = self.cells[boundary_index]
            flows.append(min(self.demand(upstream_cell), self.supply(downstream_cell)))
        return flows

    def validate_boundary_flow_or_raise(
        self,
        *,
        flow: float,
        limit: float,
        label: str,
    ) -> float:
        if not math.isfinite(flow):
            raise CTMStateError(f"{label}: граничный поток должен быть конечным числом, получено {flow}")
        if flow < -EPS:
            raise CTMStateError(f"{label}: граничный поток не может быть отрицательным, получено {flow}")
        if flow - limit > EPS:
            raise CTMStateError(
                f"{label}: граничный поток превышает CTM-ограничение: {flow} > {limit}"
            )
        return max(0.0, flow)

    def _advance_with_flows(self, flows: list[float]) -> dict[str, float | list[float]]:
        if len(flows) != self.cell_count + 1:
            raise CTMStateError(
                f"Ожидалось {self.cell_count + 1} граничных потоков, получено {len(flows)}"
            )

        before_densities = [cell.density for cell in self.cells]
        self.validate_state_or_raise(before_densities, context="до шага")

        before_total = self.total_occupancy()

        next_densities: list[float] = []
        for i, cell in enumerate(self.cells):
            inflow = flows[i]
            outflow = flows[i + 1]
            density_next = cell.density + (self.dt / cell.length) * (inflow - outflow)
            next_densities.append(density_next)

        if self.strict:
            self.validate_state_or_raise(next_densities, context="после шага")
        else:
            next_densities = [
                min(max(0.0, density), self.diagram.jam_density)
                for density in next_densities
            ]

        for cell, density in zip(self.cells, next_densities):
            cell.density = density

        self.time += self.dt

        after_total = self.total_occupancy()
        conservation_error = after_total - (
            before_total + flows[0] * self.dt - flows[-1] * self.dt
        )

        return {
            "time": self.time,
            "flows_pcu_s": flows,
            "densities_pcu_m": self.densities(),
            "occupancies_pcu": self.occupancies(),
            "external_queue_pcu": self.external_queue,
            "total_occupancy_pcu": after_total,
            "conservation_error_pcu": conservation_error,
        }

    def step_with_boundary_flows(
        self,
        *,
        upstream_flow: float,
        downstream_flow: float,
    ) -> dict[str, float | list[float]]:
        if self.validate_cfl:
            self.validate_cfl_or_raise()

        self.apply_incidents()

        upstream_flow = self.validate_boundary_flow_or_raise(
            flow=upstream_flow,
            limit=self.supply(self.cells[0]),
            label="upstream",
        )
        downstream_flow = self.validate_boundary_flow_or_raise(
            flow=downstream_flow,
            limit=self.demand(self.cells[-1]),
            label="downstream",
        )

        flows = [upstream_flow] + self.compute_internal_flows() + [downstream_flow]
        return self._advance_with_flows(flows)

    def validate_state_or_raise(self, densities: list[float], *, context: str) -> None:
        for index, density in enumerate(densities):
            if density < -EPS:
                raise CTMStateError(
                    f"{context}: отрицательная плотность в ячейке {index}: {density}"
                )
            if density - self.diagram.jam_density > EPS:
                raise CTMStateError(
                    f"{context}: плотность выше плотности затора в ячейке {index}: "
                    f"{density} > {self.diagram.jam_density}"
                )

    def step(self) -> dict[str, float | list[float]]:
        if self.validate_cfl:
            self.validate_cfl_or_raise()

        self.apply_incidents()
        flows = self.compute_boundary_flows()
        diagnostics = self._advance_with_flows(flows)

        admitted_upstream_pcu = flows[0] * self.dt
        self.external_queue = max(0.0, self.external_queue - admitted_upstream_pcu)
        diagnostics["external_queue_pcu"] = self.external_queue
        return diagnostics

    def densities(self) -> list[float]:
        return [cell.density for cell in self.cells]

    def densities_pcu_km(self) -> list[float]:
        return [cell.density * 1000.0 for cell in self.cells]

    def occupancies(self) -> list[float]:
        return [cell.occupancy for cell in self.cells]

    def total_occupancy(self) -> float:
        return sum(self.occupancies())


def demo() -> None:
    diagram = TriangularFundamentalDiagram.from_common_units(
        free_flow_speed_kph=60.0,
        backward_wave_speed_kph=18.0,
        capacity_pcu_h=1800.0,
        jam_density_pcu_km=150.0,
    )

    model = CTMModel.create_uniform_link(
        length=1000.0,
        cell_length=50.0,
        diagram=diagram,
        dt=1.0,
        initial_density=10.0 / 1000.0,
        upstream_demand=lambda _t: 0.7,
        downstream_capacity=lambda _t: 0.5,
        incidents=[
            Incident(
                cell_index=10,
                start_time=60.0,
                end_time=180.0,
                capacity_factor=0.4,
                speed_factor=0.6,
            )
        ],
        strict=True,
        validate_cfl=True,
    )

    diagnostics = {}
    for _ in range(120):
        diagnostics = model.step()

    print("Время:", diagnostics["time"])
    print("Масса в link:", diagnostics["total_occupancy_pcu"])
    print("Внешняя очередь:", diagnostics["external_queue_pcu"])
    print("Ошибка сохранения:", diagnostics["conservation_error_pcu"])
    print("Первые 5 плотностей, pcu/км:", model.densities_pcu_km()[:5])


if __name__ == "__main__":
    demo()
