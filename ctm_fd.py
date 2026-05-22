from __future__ import annotations

from dataclasses import dataclass
import math

from ctm_network_core_v2 import CTMConfigurationError, TriangularFundamentalDiagram


@dataclass(frozen=True)
class FundamentalDiagramMetadata:
    """Метаданные фундаментальной диаграммы для записи в результаты."""

    parameterization: str
    free_flow_speed_kph: float
    backward_wave_speed_kph: float
    capacity_pcu_h: float
    jam_density_pcu_km: float
    critical_density_pcu_km: float
    notes: str


def compute_backward_wave_speed_kph(
    *,
    free_flow_speed_kph: float,
    capacity_pcu_h: float,
    jam_density_pcu_km: float,
) -> float:
    """Вычисляет скорость обратной волны для согласованной треугольной FD.

    Для треугольной фундаментальной диаграммы:
        Q = v * w / (v + w) * rho_jam

    Если v, Q и rho_jam считаются входными сценарными параметрами, то:
        rho_crit = Q / v
        w = Q / (rho_jam - rho_crit)

    Все входные величины заданы в привычных транспортных единицах: км/ч,
    pcu/ч, pcu/км. Возвращаемое значение w — положительный модуль скорости
    обратной волны затора в км/ч.
    """

    if free_flow_speed_kph <= 0.0:
        raise CTMConfigurationError("free_flow_speed_kph must be positive")
    if capacity_pcu_h <= 0.0:
        raise CTMConfigurationError("capacity_pcu_h must be positive")
    if jam_density_pcu_km <= 0.0:
        raise CTMConfigurationError("jam_density_pcu_km must be positive")

    critical_density = capacity_pcu_h / free_flow_speed_kph
    if critical_density >= jam_density_pcu_km:
        raise CTMConfigurationError(
            "capacity is too high for the chosen free-flow speed and jam density: "
            f"rho_crit={critical_density:.6g} pcu/km, "
            f"rho_jam={jam_density_pcu_km:.6g} pcu/km"
        )

    backward_wave_speed = capacity_pcu_h / (jam_density_pcu_km - critical_density)
    if not math.isfinite(backward_wave_speed) or backward_wave_speed <= 0.0:
        raise CTMConfigurationError("computed backward_wave_speed_kph is not positive and finite")
    return backward_wave_speed


def make_triangular_fd_from_capacity(
    *,
    free_flow_speed_kph: float,
    capacity_pcu_h: float,
    jam_density_pcu_km: float,
) -> tuple[TriangularFundamentalDiagram, FundamentalDiagramMetadata]:
    """Создаёт самосогласованную треугольную FD из v, Q и rho_jam.

    Это предпочтительная параметризация для текущего проекта: пользователь
    задаёт интерпретируемые входные параметры — скорость свободного потока,
    пропускную способность link и jam density. Скорость обратной волны не
    выбирается отдельной эвристикой, а выводится из формулы треугольной FD.
    """

    backward_wave_speed_kph = compute_backward_wave_speed_kph(
        free_flow_speed_kph=free_flow_speed_kph,
        capacity_pcu_h=capacity_pcu_h,
        jam_density_pcu_km=jam_density_pcu_km,
    )
    diagram = TriangularFundamentalDiagram.from_common_units(
        free_flow_speed_kph=free_flow_speed_kph,
        backward_wave_speed_kph=backward_wave_speed_kph,
        capacity_pcu_h=capacity_pcu_h,
        jam_density_pcu_km=jam_density_pcu_km,
    )
    metadata = FundamentalDiagramMetadata(
        parameterization="v_Q_rhojam_with_derived_w",
        free_flow_speed_kph=free_flow_speed_kph,
        backward_wave_speed_kph=backward_wave_speed_kph,
        capacity_pcu_h=capacity_pcu_h,
        jam_density_pcu_km=jam_density_pcu_km,
        critical_density_pcu_km=capacity_pcu_h / free_flow_speed_kph,
        notes="Backward wave speed is derived from Q = v*w/(v+w)*rho_jam.",
    )
    return diagram, metadata


def make_triangular_fd_with_explicit_w(
    *,
    free_flow_speed_kph: float,
    backward_wave_speed_kph: float,
    jam_density_pcu_km: float,
) -> tuple[TriangularFundamentalDiagram, FundamentalDiagramMetadata]:
    """Создаёт треугольную FD из v, w и rho_jam, выводя capacity.

    Оставлено для экспериментов, где именно скорость обратной волны считается
    доверенным входным параметром. Такой режим всё равно не выбирает все четыре
    параметра FD независимо: capacity выводится из той же треугольной связи.
    """

    if free_flow_speed_kph <= 0.0:
        raise CTMConfigurationError("free_flow_speed_kph must be positive")
    if backward_wave_speed_kph <= 0.0:
        raise CTMConfigurationError("backward_wave_speed_kph must be positive")
    if jam_density_pcu_km <= 0.0:
        raise CTMConfigurationError("jam_density_pcu_km must be positive")

    capacity_pcu_h = (
        free_flow_speed_kph
        * backward_wave_speed_kph
        / (free_flow_speed_kph + backward_wave_speed_kph)
        * jam_density_pcu_km
    )
    diagram = TriangularFundamentalDiagram.from_common_units(
        free_flow_speed_kph=free_flow_speed_kph,
        backward_wave_speed_kph=backward_wave_speed_kph,
        capacity_pcu_h=capacity_pcu_h,
        jam_density_pcu_km=jam_density_pcu_km,
    )
    metadata = FundamentalDiagramMetadata(
        parameterization="v_w_rhojam_with_derived_Q",
        free_flow_speed_kph=free_flow_speed_kph,
        backward_wave_speed_kph=backward_wave_speed_kph,
        capacity_pcu_h=capacity_pcu_h,
        jam_density_pcu_km=jam_density_pcu_km,
        critical_density_pcu_km=capacity_pcu_h / free_flow_speed_kph,
        notes="Capacity is derived from Q = v*w/(v+w)*rho_jam.",
    )
    return diagram, metadata
