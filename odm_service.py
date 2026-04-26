from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from models import Link, Project


KT_DEFAULT = 0.04
KN_DEFAULT = 0.143
KG_DEFAULT = 0.0834
KRCH_30TH_DEFAULT = 1.275
TARGET_LOS_VC = 0.70
BASE_SPEED_KPH = 60.0
B_COEFFICIENT = 4

LOS_THRESHOLDS = {
    "A": 0.20,
    "B": 0.45,
    "C": 0.70,
    "D": 0.90,
    "E": 1.00,
    "F": float("inf"),
}

HOURLY_MODE_AVG = "avg_hour"
HOURLY_MODE_DESIGN = "design_hour"
DEFAULT_HOURLY_MODE = HOURLY_MODE_DESIGN
DESIGN_HOUR_DAILY_SPLIT_TOTAL = 0.076
DESIGN_HOUR_PEAK_SHARE_DEFAULT = 0.15


@dataclass(frozen=True)
class OdmCapacity:
    pmax_base: float
    n_effective: int
    capacity: float
    factors: dict[str, float]
    defaults_used: list[str]


def analyze_odm_city_link(
    link: Link,
    pcu_coeffs: dict[str, float],
    hourly_mode: str = DEFAULT_HOURLY_MODE,
) -> dict[str, Any] | None:
    skdf = (link.metadata or {}).get("skdf") or {}
    aadt = _float_value(skdf.get("traffic_aadt"))
    if aadt is None:
        aadt = _float_value(skdf.get("traffic"))
    if aadt is None:
        aadt = _float_value(_first_value(skdf.get("traffic_values")))
    if aadt is None:
        return None

    n_hour_avg = derive_average_hourly_intensity(aadt)
    n_hour_design_base = derive_design_hourly_intensity(aadt)
    n_hour_design_split = derive_design_hourly_intensity_from_daily_split(aadt)
    n_hour_design_peak = derive_design_hourly_intensity_from_peak_share(link, aadt)
    n_hour_design = max(n_hour_design_base, n_hour_design_split, n_hour_design_peak)
    pcu_multiplier = _pcu_multiplier(link, pcu_coeffs)
    v_avg = n_hour_avg * pcu_multiplier
    v_design = n_hour_design * pcu_multiplier

    capacity = calculate_city_capacity(link)
    vc_ratio_avg = _safe_divide(v_avg, capacity.capacity)
    vc_ratio_design = _safe_divide(v_design, capacity.capacity)
    los_avg = determine_los(vc_ratio_avg)
    los_design = determine_los(vc_ratio_design)
    delay_avg = calculate_delay(link.length_km, vc_ratio_avg, capacity.capacity)
    delay_design = calculate_delay(link.length_km, vc_ratio_design, capacity.capacity)

    active_mode = HOURLY_MODE_AVG if hourly_mode == HOURLY_MODE_AVG else HOURLY_MODE_DESIGN
    active_v = v_avg if active_mode == HOURLY_MODE_AVG else v_design
    active_ratio = vc_ratio_avg if active_mode == HOURLY_MODE_AVG else vc_ratio_design
    active_los = los_avg if active_mode == HOURLY_MODE_AVG else los_design
    active_delay = delay_avg if active_mode == HOURLY_MODE_AVG else delay_design

    results = {
        "id": link.id,
        "name": link.name,
        "type": "OdmCityRoad",
        "hourly_mode": active_mode,
        "N_day_raw": round(aadt, 3),
        "N_hour_avg": round(n_hour_avg, 3),
        "N_hour_design_base": round(n_hour_design_base, 3),
        "N_hour_design_split_total": round(n_hour_design_split, 3),
        "N_hour_design_peak_share": round(n_hour_design_peak, 3),
        "N_hour_design": round(n_hour_design, 3),
        "V_avg": round(v_avg, 3),
        "V_design": round(v_design, 3),
        "P_odm": round(capacity.capacity, 3),
        "Pmax_base": round(capacity.pmax_base, 3),
        "pmax_lane_factor": capacity.n_effective,
        "VC_ratio_avg": round(vc_ratio_avg, 3),
        "VC_ratio_design": round(vc_ratio_design, 3),
        "LOS_avg": los_avg,
        "LOS_design": los_design,
        "Delay_avg_sec": round(delay_avg, 1),
        "Delay_design_sec": round(delay_design, 1),
        "capacity_skdf_reference": skdf.get("capacity_values", skdf.get("capacity_total", skdf.get("capacity", []))),
        "odm_defaults_used": capacity.defaults_used,
        "odm_factors": {name: round(value, 4) for name, value in capacity.factors.items()},
        "N_directional_estimates": {
            "forward_estimate": round(0.046 * aadt, 3),
            "reverse_estimate": round(0.03 * aadt, 3),
        },
        "design_hour_method": (
            "peak_share"
            if n_hour_design_peak >= max(n_hour_design_base, n_hour_design_split)
            else (
                "daily_split_total"
                if n_hour_design_split >= n_hour_design_base
                else "appendix_b"
            )
        ),
        "Length_km": link.length_km,
        "V": round(active_v, 3),
        "C_initial": round(capacity.capacity, 3),
        "VC_ratio": round(active_ratio, 3),
        "LOS": active_los,
        "Delay_sec": round(active_delay, 1),
    }

    optimization = optimize_capacity(
        active_v=active_v,
        capacity=capacity.capacity,
        lane_count=_effective_lane_count(link),
        link_type=link.link_type,
    )
    if optimization:
        results.update(optimization)

    return results


def calculate_city_capacity(link: Link) -> OdmCapacity:
    params = link.parameters or {}
    defaults_used: list[str] = []

    pmax_base, n_effective, pmax_defaults = get_pmax_lookup(link)
    defaults_used.extend(pmax_defaults)

    lane_width = _float_value(params.get("lane_width_m"))
    if lane_width is None:
        lane_width = 3.5
        defaults_used.append("lane_width")
    fb = max(0.7, min(1.2, 1.0 + (lane_width - 3.6) / 9.0))

    heavy_share = _heavy_vehicle_share(link)
    fgr, fgr_default = heavy_vehicle_factor(heavy_share)
    if fgr_default:
        defaults_used.append("fgr")

    grade = _float_value(params.get("grade_percent"))
    if grade is None:
        grade = 0.0
        defaults_used.append("fi")
    fi = max(0.7, min(1.1, 1.0 - grade / 200.0))

    fp, fp_default = parking_factor(link)
    if fp_default:
        defaults_used.append("fp")

    favt, favt_default = bus_stop_factor(link, _effective_lane_count(link))
    if favt_default:
        defaults_used.append("favt")

    fter, fter_default = territory_factor(link)
    if fter_default:
        defaults_used.append("fter")

    fr, fr_default = radius_factor(link)
    if fr_default:
        defaults_used.append("fR")

    fv, fv_default = speed_limit_factor(link)
    if fv_default:
        defaults_used.append("fV")

    factors = {
        "fb": fb,
        "fgr": fgr,
        "fi": fi,
        "fp": fp,
        "favt": favt,
        "fter": fter,
        "fR": fr,
        "fV": fv,
    }

    capacity = pmax_base * n_effective
    for value in factors.values():
        capacity *= value

    return OdmCapacity(
        pmax_base=pmax_base,
        n_effective=n_effective,
        capacity=max(capacity, 1.0),
        factors=factors,
        defaults_used=sorted(set(defaults_used)),
    )


def derive_average_hourly_intensity(aadt: float) -> float:
    return aadt * 365.0 * KT_DEFAULT * KN_DEFAULT * KG_DEFAULT / 4.0


def derive_design_hourly_intensity(aadt: float) -> float:
    return derive_average_hourly_intensity(aadt) * KRCH_30TH_DEFAULT


def derive_design_hourly_intensity_from_daily_split(aadt: float) -> float:
    return aadt * DESIGN_HOUR_DAILY_SPLIT_TOTAL


def derive_design_hourly_intensity_from_peak_share(link: Link, aadt: float) -> float:
    share = _float_value((link.metadata or {}).get("peak_hour_share"))
    if share is None:
        share = _float_value((link.parameters or {}).get("peak_hour_share"))
    if share is None:
        share = DESIGN_HOUR_PEAK_SHARE_DEFAULT
    if share > 1:
        share /= 100.0
    share = max(min(share, 1.0), 0.0)
    return aadt * share


def determine_los(vc_ratio: float) -> str:
    for los, threshold in LOS_THRESHOLDS.items():
        if vc_ratio <= threshold:
            return los
    return "F"


def calculate_delay(length_km: float, vc_ratio: float, capacity: float) -> float:
    if capacity <= 0:
        return float("inf")
    t0 = (length_km / BASE_SPEED_KPH) * 3600 if length_km else 0.0
    if vc_ratio < 0.99:
        return t0 * 0.25 * (vc_ratio ** B_COEFFICIENT)
    return t0 * (10 + (vc_ratio * 5))


def optimize_capacity(
    active_v: float,
    capacity: float,
    lane_count: int,
    link_type: str,
) -> dict[str, Any] | None:
    if capacity <= 0 or lane_count <= 0 or link_type != "straight":
        return None

    vc_ratio = active_v / capacity
    if vc_ratio <= TARGET_LOS_VC:
        return None

    required_capacity = active_v / TARGET_LOS_VC
    per_lane_effective = capacity / lane_count
    if per_lane_effective <= 0:
        return None

    required_lanes = required_capacity / per_lane_effective
    additional_lanes = max(required_lanes - lane_count, 0.0)
    return {
        "Optimization_Proposal": f"Расширение: добавить {additional_lanes:.1f} полосы.",
        "C_optimized": round(required_capacity, 0),
        "VC_optimized": round(TARGET_LOS_VC, 3),
        "LOS_optimized": determine_los(TARGET_LOS_VC),
    }


def set_project_hourly_mode(project: Project, hourly_mode: str) -> None:
    active_mode = HOURLY_MODE_AVG if hourly_mode == HOURLY_MODE_AVG else HOURLY_MODE_DESIGN
    project.metadata = {**(project.metadata or {}), "hourly_mode": active_mode}
    for link in project.network.links.values():
        results = link.results or {}
        if "LOS_avg" not in results or "LOS_design" not in results:
            continue

        results.update(
            {
                "hourly_mode": active_mode,
                "V": results.get("V_avg") if active_mode == HOURLY_MODE_AVG else results.get("V_design"),
                "VC_ratio": results.get("VC_ratio_avg") if active_mode == HOURLY_MODE_AVG else results.get("VC_ratio_design"),
                "LOS": results.get("LOS_avg") if active_mode == HOURLY_MODE_AVG else results.get("LOS_design"),
                "Delay_sec": results.get("Delay_avg_sec") if active_mode == HOURLY_MODE_AVG else results.get("Delay_design_sec"),
            }
        )


def get_pmax_lookup(link: Link) -> tuple[float, int, list[str]]:
    defaults_used: list[str] = []
    lanes = _effective_lane_count(link)
    if lanes <= 0:
        lanes = 1
        defaults_used.append("lane_count")

    has_median = _bool_value((link.metadata or {}).get("has_median"))
    if has_median is None and lanes in {4, 6}:
        has_median = False
        defaults_used.append("median")

    if lanes == 1:
        defaults_used.append("pmax_single_lane")
        return 1800.0, 1, defaults_used
    if lanes == 2:
        return 3600.0, 1, defaults_used
    if lanes == 3:
        return 4000.0, 1, defaults_used
    if lanes == 4:
        return (2200.0 if has_median else 2100.0), 4, defaults_used
    if lanes == 6:
        return (2300.0 if has_median else 2200.0), 6, defaults_used
    if lanes >= 8:
        return 2300.0, lanes, defaults_used
    return 1800.0, lanes, defaults_used


def heavy_vehicle_factor(heavy_share: float | None) -> tuple[float, bool]:
    if heavy_share is None:
        return 1.0, True
    return 100.0 / (100.0 + heavy_share), False


def parking_factor(link: Link) -> tuple[float, bool]:
    params = link.parameters or {}
    if not _bool_value(params.get("parking_present")):
        return 1.0, True

    manoeuvres = _float_value(params.get("parking_manoeuvres_per_hour"))
    intensity = _float_value(params.get("odm_hourly_intensity_for_parking"))
    if manoeuvres is None or intensity is None or intensity <= 0:
        return 1.0, True

    lanes = _effective_lane_count(link)
    factor = (lanes - 0.1 - 18.0 * manoeuvres / 3600.0) / max(lanes, 1)
    return max(min(factor, 1.0), 0.05), False


def bus_stop_factor(link: Link, lanes: int) -> tuple[float, bool]:
    params = link.parameters or {}
    stop_layout = str(params.get("bus_stop_layout") or "").strip().lower()
    if stop_layout not in {"pocket", "lane"}:
        return 1.0, True

    stop_count = _float_value(params.get("bus_stop_count_per_hour"))
    occupancy = _float_value(params.get("bus_stop_occupancy_sec"))
    if stop_layout == "pocket" and stop_count is not None:
        factor = (lanes - 14.14 * stop_count / 3600.0) / max(lanes, 1)
        return max(min(factor, 1.0), 0.05), False
    if stop_layout == "lane" and occupancy is not None:
        factor = (lanes - occupancy / 3600.0) / max(lanes, 1)
        return max(min(factor, 1.0), 0.05), False
    return 1.0, True


def territory_factor(link: Link) -> tuple[float, bool]:
    territory = str(((link.metadata or {}).get("territory_type") or "")).strip().lower()
    if territory == "central":
        return 0.9, False
    if territory:
        return 1.0, False
    return 1.0, True


def radius_factor(link: Link) -> tuple[float, bool]:
    radius = estimate_curve_radius_m(link)
    if radius is None:
        return 1.0, True
    if radius < 100:
        return 0.85, False
    if radius < 250:
        return 0.90, False
    if radius < 450:
        return 0.96, False
    if radius < 600:
        return 0.99, False
    return 1.0, False


def speed_limit_factor(link: Link) -> tuple[float, bool]:
    speed_limit = _speed_limit_value(link)
    used_default = speed_limit is None
    if speed_limit is None:
        speed_limit = 60.0
    if speed_limit <= 10:
        return 0.44, used_default
    if speed_limit <= 20:
        return 0.76, used_default
    if speed_limit <= 30:
        return 0.88, used_default
    if speed_limit <= 40:
        return 0.96, used_default
    if speed_limit <= 50:
        return 0.98, used_default
    return 1.0, used_default


def estimate_curve_radius_m(link: Link) -> float | None:
    points = _projected_points_for_link(link)
    if len(points) < 3:
        return None

    radii = []
    for p1, p2, p3 in zip(points, points[1:], points[2:]):
        radius = _circumradius(p1, p2, p3)
        if radius is not None and math.isfinite(radius):
            radii.append(radius)
    if not radii:
        return None
    return min(radii)


def _projected_points_for_link(link: Link) -> list[tuple[float, float]]:
    coords = link.coords or {}
    if coords.get("type") == "polyline":
        raw_points = coords.get("points", [])
    else:
        raw_points = [
            (coords.get("lon_start"), coords.get("lat_start")),
            (coords.get("lon_end"), coords.get("lat_end")),
        ]

    clean_points = []
    for lon, lat in raw_points:
        if lon is None or lat is None:
            continue
        clean_points.append((float(lon), float(lat)))
    if len(clean_points) < 2:
        return []

    try:
        from pyproj import Transformer
    except ImportError:
        return []

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    return [transformer.transform(lon, lat) for lon, lat in clean_points]


def _circumradius(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> float | None:
    a = _distance(p2, p3)
    b = _distance(p1, p3)
    c = _distance(p1, p2)
    area2 = abs(
        p1[0] * (p2[1] - p3[1])
        + p2[0] * (p3[1] - p1[1])
        + p3[0] * (p1[1] - p2[1])
    )
    if area2 <= 1e-6:
        return None
    area = area2 / 2.0
    return (a * b * c) / (4.0 * area)


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _speed_limit_value(link: Link) -> float | None:
    params = link.parameters or {}
    skdf = (link.metadata or {}).get("skdf") or {}
    for value in (
        params.get("speed_limit_skdf"),
        _first_value(skdf.get("speed_limit_values")),
        params.get("speed_limit"),
        (link.metadata or {}).get("maxspeed"),
    ):
        parsed = _float_value(value)
        if parsed is not None:
            return parsed
    return None


def _effective_lane_count(link: Link) -> int:
    params = link.parameters or {}
    lanes = _int_value(params.get("lanes_total") or params.get("lanes_count") or 1)
    lanes = max(lanes or 1, 1)
    oneway = _bool_value((link.metadata or {}).get("oneway"))
    if oneway:
        return lanes
    return max((lanes + 1) // 2, 1)


def _heavy_vehicle_share(link: Link) -> float | None:
    params = link.parameters or {}
    heavy = _float_value(params.get("heavy_vehicles_percent"))
    if heavy is None:
        return None
    if heavy <= 1:
        return max(heavy * 100.0, 0.0)
    return max(heavy, 0.0)


def _pcu_multiplier(link: Link, pcu_coeffs: dict[str, float]) -> float:
    heavy_share = _heavy_vehicle_share(link) or 0.0
    heavy_fraction = max(min(heavy_share / 100.0, 1.0), 0.0)

    car_coeff = float(pcu_coeffs.get("car", pcu_coeffs.get("passenger_car", 1.0)))
    heavy_candidates = [
        pcu_coeffs.get("truck"),
        pcu_coeffs.get("truck_lt_3_5t"),
        pcu_coeffs.get("truck_lt_10t"),
        pcu_coeffs.get("bus"),
        pcu_coeffs.get("other"),
    ]
    heavy_coeffs = [float(value) for value in heavy_candidates if value is not None]
    heavy_coeff = sum(heavy_coeffs) / len(heavy_coeffs) if heavy_coeffs else car_coeff

    return ((1.0 - heavy_fraction) * car_coeff) + (heavy_fraction * heavy_coeff)


def _safe_divide(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 99.0
    return numerator / denominator


def _float_value(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)

    text = str(value).strip().replace(",", ".")
    if not text:
        return None
    if text.startswith("[") and text.endswith("]"):
        try:
            values = [float(item) for item in text.strip("[]").split(",") if item.strip()]
        except ValueError:
            return None
        return values[0] if values else None

    try:
        return float(text)
    except ValueError:
        digits = "".join(ch for ch in text if ch.isdigit() or ch in {".", "-"})
        if not digits:
            return None
        try:
            return float(digits)
        except ValueError:
            return None


def _int_value(value: Any) -> int | None:
    parsed = _float_value(value)
    if parsed is None:
        return None
    return int(round(parsed))


def _bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _first_value(value: Any) -> Any:
    if isinstance(value, list) and value:
        return value[0]
    return value
