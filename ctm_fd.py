from __future__ import annotations

from dataclasses import dataclass
import math

from ctm_network_core_v2 import CTMConfigurationError, TriangularFundamentalDiagram


@dataclass(frozen=True)
class FundamentalDiagramMetadata:
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
    """Compute congestion wave speed for a consistent triangular FD.

    For a triangular fundamental diagram:
        Q = v * w / (v + w) * rho_jam

    Equivalently, if v, Q and rho_jam are treated as scenario inputs:
        rho_crit = Q / v
        w = Q / (rho_jam - rho_crit)

    All units are common traffic units: km/h, pcu/h, pcu/km. The returned w is
    the positive magnitude of the backward congestion wave speed in km/h.
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
    """Build a self-consistent triangular FD from v, Q and rho_jam.

    This is the preferred parameterization in the current project because the
    user can choose interpretable scenario inputs: free-flow speed, link capacity
    and jam density. The backward wave speed is then not another independent
    heuristic coefficient; it is derived from the triangular FD equation.
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
    """Build a triangular FD from v, w and rho_jam by deriving capacity.

    Kept for experiments where the wave speed is the trusted input. It avoids
    independently choosing all four FD parameters.
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
