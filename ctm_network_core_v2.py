"""
ctm_network_core_v2.py

Чистое CTM-ядро по обновлённому плану:
- оставлен только CTMModel;
- LegacyHydrodynamicModel удалён;
- используется треугольная фундаментальная диаграмма;
- потоки считаются как физические rates (pcu/s);
- плотности хранятся как переменные состояния (pcu/m);
- опциональная входная точечная очередь предотвращает потерю спроса;
- strict mode запрещает молчаливый clipping и потерю массы;
- CFL-проверка восстановлена;
- поддерживаются аварийные ограничения и fixed-cycle signals.

Модель намеренно описывает один направленный link, разбитый на ячейки.
Методы граничных потоков устроены так, чтобы в будущем внешний Node/Junction
класс мог заменить upstream/downstream boundary logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Callable, Optional


EPS = 1e-9


class CTMConfigurationError(ValueError):
    """Ошибка конфигурации: параметры CTM нарушают физические или численные ограничения."""


class CTMStateError(RuntimeError):
    """Ошибка состояния: шаг симуляции дал физически недопустимое состояние."""


@dataclass(frozen=True)
class TriangularFundamentalDiagram:
    """Треугольная фундаментальная диаграмма для одного направленного link.

    Единицы:
    - free_flow_speed: м/с;
    - backward_wave_speed: м/с, положительный модуль обратной волны затора;
    - capacity: pcu/s для данного направленного link;
    - jam_density: pcu/m для данного направленного link.

    Demand и supply представлены как rates:

        demand_i = min(v * rho_i, Q)
        supply_i = min(Q, w * (rho_jam - rho_i))

    где:
    - v — скорость свободного потока;
    - w — модуль скорости обратной волны;
    - Q — пропускная способность;
    - rho_jam — jam density.
    """

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
        """Критическая плотность в pcu/m."""
        return self.capacity / self.free_flow_speed

    def validate(self) -> None:
        if self.free_flow_speed <= 0:
            raise CTMConfigurationError("free_flow_speed must be positive")
        if self.backward_wave_speed <= 0:
            raise CTMConfigurationError("backward_wave_speed must be positive")
        if self.capacity <= 0:
            raise CTMConfigurationError("capacity must be positive")
        if self.jam_density <= 0:
            raise CTMConfigurationError("jam_density must be positive")
        if self.critical_density >= self.jam_density:
            raise CTMConfigurationError(
                "critical_density must be below jam_density; check capacity, "
                "free_flow_speed, and jam_density"
            )


@dataclass
class Cell:
    """Одна CTM-ячейка.

    Основная переменная состояния — плотность в pcu/m. `capacity_factor` и
    `speed_factor` позволяют задавать локальные ограничения, например аварию.
    """

    length: float
    density: float = 0.0
    capacity_factor: float = 1.0
    speed_factor: float = 1.0

    @property
    def occupancy(self) -> float:
        """Количество passenger-car units внутри ячейки."""
        return self.density * self.length

    def set_occupancy(self, occupancy: float) -> None:
        self.density = occupancy / self.length


@dataclass(frozen=True)
class Incident:
    """Временное локальное ограничение внутри одной ячейки.

    `capacity_factor` умножает capacity фундаментальной диаграммы.
    `speed_factor` умножает free-flow speed при расчёте demand.
    """

    cell_index: int
    start_time: float
    end_time: float
    capacity_factor: float = 1.0
    speed_factor: float = 1.0

    def is_active(self, time: float) -> bool:
        return self.start_time <= time < self.end_time


@dataclass(frozen=True)
class FixedCycleSignal:
    """Fixed-cycle светофор на границе между ячейками.

    `boundary_index` следует соглашению списка boundary flows:
    - 0 означает upstream boundary в cell 0;
    - i означает границу между cell i-1 и cell i;
    - cell_count означает downstream boundary после последней ячейки.

    Для простых внутренних светофоров используйте boundary_index в диапазоне
    [1, cell_count - 1].
    """

    boundary_index: int
    green_duration: float
    red_duration: float
    offset: float = 0.0

    def green_fraction(self, time: float) -> float:
        cycle = self.green_duration + self.red_duration
        if cycle <= 0:
            raise CTMConfigurationError("signal cycle must be positive")
        phase = (time - self.offset) % cycle
        return 1.0 if phase < self.green_duration else 0.0


@dataclass
class CTMModel:
    """Однозвенная CTM-модель с rates, очередью, авариями и светофорами.

    Переменная состояния:
    - density в каждой ячейке, pcu/m.

    Граничные и внутренние потоки:
    - rates в pcu/s.

    Уравнение обновления:
        rho_i(t+dt) = rho_i(t) + dt / L_i * (inflow_i - outflow_i)

    Реализация рассчитана на дальнейшее расширение: upstream_demand и
    downstream_capacity можно заменить сетевой Node/Junction-моделью.
    """

    cells: list[Cell]
    diagram: TriangularFundamentalDiagram
    dt: float
    time: float = 0.0

    upstream_demand: Callable[[float], float] = lambda _time: 0.0  # pcu/s
    downstream_capacity: Callable[[float], float] = lambda _time: float("inf")  # pcu/s

    incidents: list[Incident] = field(default_factory=list)
    signals: list[FixedCycleSignal] = field(default_factory=list)

    external_queue: float = 0.0  # pcu, ожидающие перед upstream boundary
    strict: bool = True
    validate_cfl: bool = True

    def __post_init__(self) -> None:
        self.diagram.validate()
        if not self.cells:
            raise CTMConfigurationError("CTMModel requires at least one cell")
        if self.dt <= 0:
            raise CTMConfigurationError("dt must be positive")
        for cell in self.cells:
            if cell.length <= 0:
                raise CTMConfigurationError("cell.length must be positive")
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
            raise CTMConfigurationError("length must be positive")
        if cell_length <= 0:
            raise CTMConfigurationError("cell_length must be positive")
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
        """CFL-безопасный временной шаг для самой короткой ячейки."""
        min_length = min(cell.length for cell in self.cells)
        return min_length / max(
            self.diagram.free_flow_speed,
            self.diagram.backward_wave_speed,
        )

    def validate_cfl_or_raise(self) -> None:
        max_dt = self.max_stable_dt()
        if self.dt - max_dt > EPS:
            raise CTMConfigurationError(
                f"CFL condition violated: dt={self.dt:.6g}s, "
                f"max_dt={max_dt:.6g}s. Reduce dt or increase cell length."
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
                    f"incident cell_index={incident.cell_index} outside cell range"
                )
            if incident.is_active(self.time):
                cell = self.cells[incident.cell_index]
                cell.capacity_factor *= incident.capacity_factor
                cell.speed_factor *= incident.speed_factor

    def capacity_of(self, cell: Cell) -> float:
        return max(0.0, self.diagram.capacity * cell.capacity_factor)

    def demand(self, cell: Cell) -> float:
        """Sending/demand rate из ячейки, pcu/s."""
        free_flow = (
            self.diagram.free_flow_speed
            * max(0.0, cell.speed_factor)
            * max(0.0, cell.density)
        )
        return min(free_flow, self.capacity_of(cell))

    def supply(self, cell: Cell) -> float:
        """Receiving/supply rate в ячейку, pcu/s."""
        available_density = max(0.0, self.diagram.jam_density - cell.density)
        congested_branch = self.diagram.backward_wave_speed * available_density
        return min(congested_branch, self.capacity_of(cell))

    def signal_factor(self, boundary_index: int) -> float:
        factor = 1.0
        for signal in self.signals:
            if signal.boundary_index == boundary_index:
                factor *= signal.green_fraction(self.time)
        return factor

    def compute_boundary_flows(self) -> list[float]:
        """Считает все boundary flows как rates в pcu/s.

        Возвращает список длины cell_count + 1:
        - flows[0]: upstream boundary в первую ячейку;
        - flows[i]: внутренняя граница из cell i-1 в cell i;
        - flows[cell_count]: downstream boundary из последней ячейки.
        """

        cells = self.cells
        flows: list[float] = []

        # Upstream boundary с точечной внешней очередью.
        new_external_demand = max(0.0, self.upstream_demand(self.time)) * self.dt
        self.external_queue += new_external_demand

        first_supply_rate = self.supply(cells[0]) * self.signal_factor(0)
        queued_demand_rate = self.external_queue / self.dt
        upstream_flow = min(queued_demand_rate, first_supply_rate)
        flows.append(upstream_flow)

        # Внутренние границы между ячейками.
        flows.extend(self.compute_internal_flows())

        # Downstream boundary.
        downstream_flow = min(
            self.demand(cells[-1]),
            max(0.0, self.downstream_capacity(self.time)),
        )
        downstream_flow *= self.signal_factor(self.cell_count)
        flows.append(downstream_flow)

        return flows

    def compute_internal_flows(self) -> list[float]:
        """Считает внутренние cell-to-cell flows как rates в pcu/s."""
        flows: list[float] = []
        for boundary_index in range(1, self.cell_count):
            upstream_cell = self.cells[boundary_index - 1]
            downstream_cell = self.cells[boundary_index]
            flow = min(
                self.demand(upstream_cell),
                self.supply(downstream_cell),
            )
            flow *= self.signal_factor(boundary_index)
            flows.append(flow)
        return flows

    def validate_boundary_flow_or_raise(
        self,
        *,
        flow: float,
        limit: float,
        label: str,
    ) -> float:
        """Проверяет внешний network-solved boundary flow."""
        if not math.isfinite(flow):
            raise CTMStateError(f"{label} boundary flow must be finite: {flow}")
        if flow < -EPS:
            raise CTMStateError(f"{label} boundary flow must be non-negative: {flow}")
        if flow - limit > EPS:
            raise CTMStateError(
                f"{label} boundary flow exceeds CTM limit: {flow} > {limit}"
            )
        return max(0.0, flow)

    def _advance_with_flows(self, flows: list[float]) -> dict[str, float | list[float]]:
        """Продвигает состояние по уже рассчитанным boundary/internal CTM flows."""
        if len(flows) != self.cell_count + 1:
            raise CTMStateError(
                f"expected {self.cell_count + 1} boundary flows, got {len(flows)}"
            )

        before_densities = [cell.density for cell in self.cells]
        self.validate_state_or_raise(before_densities, context="before step")

        before_total = self.total_occupancy()

        next_densities: list[float] = []
        for i, cell in enumerate(self.cells):
            inflow = flows[i]
            outflow = flows[i + 1]
            density_next = cell.density + (self.dt / cell.length) * (inflow - outflow)
            next_densities.append(density_next)

        if self.strict:
            self.validate_state_or_raise(next_densities, context="after step")
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
        """Выполняет один шаг с network-solved upstream/downstream flows.

        Метод предназначен для сетевых CTM-симуляций, где отдельный node solver
        определяет граничные потоки link. Сам link всё равно отвечает за
        консервативное CTM-обновление и внутренние cell-to-cell flows.
        """

        if self.validate_cfl:
            self.validate_cfl_or_raise()

        self.apply_incidents()

        upstream_limit = self.supply(self.cells[0]) * self.signal_factor(0)
        downstream_limit = self.demand(self.cells[-1]) * self.signal_factor(self.cell_count)
        upstream_flow = self.validate_boundary_flow_or_raise(
            flow=upstream_flow,
            limit=upstream_limit,
            label="upstream",
        )
        downstream_flow = self.validate_boundary_flow_or_raise(
            flow=downstream_flow,
            limit=downstream_limit,
            label="downstream",
        )

        flows = [upstream_flow] + self.compute_internal_flows() + [downstream_flow]
        return self._advance_with_flows(flows)

    def validate_state_or_raise(self, densities: list[float], *, context: str) -> None:
        for index, density in enumerate(densities):
            if density < -EPS:
                raise CTMStateError(
                    f"{context}: negative density in cell {index}: {density}"
                )
            if density - self.diagram.jam_density > EPS:
                raise CTMStateError(
                    f"{context}: density above jam density in cell {index}: "
                    f"{density} > {self.diagram.jam_density}"
                )

    def step(self) -> dict[str, float | list[float]]:
        """Выполняет один временной шаг и возвращает диагностику."""

        if self.validate_cfl:
            self.validate_cfl_or_raise()

        self.apply_incidents()
        flows = self.compute_boundary_flows()
        diagnostics = self._advance_with_flows(flows)

        # Из внешней очереди удаляется только реально допущенный upstream flow.
        admitted_upstream_vehicles = flows[0] * self.dt
        self.external_queue = max(0.0, self.external_queue - admitted_upstream_vehicles)
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
    """Маленький smoke-test сценарий."""

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
        initial_density=10.0 / 1000.0,  # 10 pcu/km
        upstream_demand=lambda _t: 0.7,  # pcu/s, намеренно выше capacity
        downstream_capacity=lambda _t: 0.5,  # pcu/s
        incidents=[
            Incident(
                cell_index=10,
                start_time=60.0,
                end_time=180.0,
                capacity_factor=0.4,
                speed_factor=0.6,
            )
        ],
        signals=[
            FixedCycleSignal(
                boundary_index=15,
                green_duration=30.0,
                red_duration=30.0,
                offset=0.0,
            )
        ],
        strict=True,
        validate_cfl=True,
    )

    for _ in range(120):
        diagnostics = model.step()

    print("time:", diagnostics["time"])
    print("total occupancy:", diagnostics["total_occupancy_pcu"])
    print("external queue:", diagnostics["external_queue_pcu"])
    print("conservation error:", diagnostics["conservation_error_pcu"])
    print("first 5 densities pcu/km:", model.densities_pcu_km()[:5])


if __name__ == "__main__":
    demo()
