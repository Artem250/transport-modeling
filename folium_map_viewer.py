from __future__ import annotations

import io
import os
import sys
from statistics import mean
from typing import Any

from PyQt5.QtWidgets import QMainWindow, QVBoxLayout, QWidget
from PyQt5.QtWebEngineWidgets import QWebEngineProfile, QWebEngineView


LOS_COLORS = {
    "A": "#1a9850",
    "B": "#66bd63",
    "C": "#fee08b",
    "D": "#fdae61",
    "E": "#f46d43",
    "F": "#d73027",
}


class FoliumMapWindow(QMainWindow):
    def __init__(self, project, title="Traffic map", skdf_csv_path: str | None = None):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(1200, 800)

        configure_webengine_profile()
        self.browser = QWebEngineView()
        self.browser.setHtml(build_project_map_html(project, skdf_csv_path=skdf_csv_path))

        layout = QVBoxLayout()
        layout.addWidget(self.browser)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)


def configure_webengine_profile() -> None:
    cache_dir = os.path.join(os.path.expanduser("~"), ".praktika_qtwebengine_cache")
    os.makedirs(cache_dir, exist_ok=True)
    profile = QWebEngineProfile.defaultProfile()
    profile.setCachePath(cache_dir)
    profile.setPersistentStoragePath(cache_dir)


def build_project_map_html(project, skdf_csv_path: str | None = None) -> str:
    try:
        import folium
    except ImportError as exc:
        raise RuntimeError(
            "Для web-карты нужен пакет folium в том же Python, которым запущено приложение.\n"
            f"Текущий Python: {sys.executable}\n"
            f"Команда установки: \"{sys.executable}\" -m pip install folium"
        ) from exc

    center = _project_center(project)
    m = folium.Map(location=center, zoom_start=14, tiles=None)
    folium.TileLayer("OpenStreetMap", name="OSM").add_to(m)
    folium.TileLayer("CartoDB positron", name="CartoDB Positron").add_to(m)

    osm_layer = folium.FeatureGroup(name="OSM graph", show=True)
    node_layer = folium.FeatureGroup(name="Network nodes", show=False)
    bounds: list[list[float]] = []

    for link in project.network.links.values():
        points = _link_latlon_points(link)
        if len(points) < 2:
            continue
        bounds.extend(points)
        folium.PolyLine(
            locations=points,
            color=_link_color(link),
            weight=_link_weight(link),
            opacity=0.85,
            tooltip=_link_tooltip(link),
        ).add_to(osm_layer)

    for node in project.network.nodes.values():
        if node.lat is None or node.lon is None:
            continue
        folium.CircleMarker(
            location=[node.lat, node.lon],
            radius=3,
            color="#263238",
            fill=True,
            fill_color="#ffffff",
            fill_opacity=0.9,
            weight=1,
            tooltip=node.name or node.id,
        ).add_to(node_layer)

    osm_layer.add_to(m)
    node_layer.add_to(m)

    if skdf_csv_path:
        skdf_layer = folium.FeatureGroup(name="SKDF roads", show=True)
        _add_skdf_layer(skdf_layer, skdf_csv_path, bounds)
        skdf_layer.add_to(m)

    if bounds:
        m.fit_bounds(bounds, padding=(20, 20))

    folium.LayerControl(collapsed=False).add_to(m)

    data = io.BytesIO()
    m.save(data, close_file=False)
    html = data.getvalue().decode("utf-8")
    return html.replace("<head>", '<head><meta name="referrer" content="no-referrer-when-downgrade">')


def _project_center(project) -> list[float]:
    points = []
    for node in project.network.nodes.values():
        if node.lat is not None and node.lon is not None:
            points.append((node.lat, node.lon))
    if points:
        return [mean(lat for lat, _ in points), mean(lon for _, lon in points)]

    for link in project.network.links.values():
        for lat, lon in _link_latlon_points(link):
            points.append((lat, lon))
    if points:
        return [mean(lat for lat, _ in points), mean(lon for _, lon in points)]
    return [54.841, 83.106]


def _link_latlon_points(link) -> list[list[float]]:
    coords = link.coords or {}
    if coords.get("type") == "polyline":
        return [[lat, lon] for lon, lat in coords.get("points", []) if lon is not None and lat is not None]

    lon_start = coords.get("lon_start")
    lat_start = coords.get("lat_start")
    lon_end = coords.get("lon_end")
    lat_end = coords.get("lat_end")
    if None in (lon_start, lat_start, lon_end, lat_end):
        return []
    return [[lat_start, lon_start], [lat_end, lon_end]]


def _link_color(link) -> str:
    results = link.results or {}
    los = results.get("LOS")
    if los in LOS_COLORS:
        return LOS_COLORS[los]

    vc_ratio = _float_value(results.get("VC_ratio"))
    if vc_ratio is None:
        intensity = _float_value((link.traffic_counts or {}).get("car"))
        capacity = _capacity(link)
        vc_ratio = intensity / capacity if intensity is not None and capacity else None
    if vc_ratio is None:
        return "#1976d2"
    if vc_ratio > 1:
        return "#d73027"
    if vc_ratio > 0.8:
        return "#f46d43"
    if vc_ratio > 0.6:
        return "#fdae61"
    return "#1976d2"


def _link_weight(link) -> int:
    params = link.parameters or {}
    lanes = params.get("lanes_total") or params.get("lanes_count") or 1
    try:
        return max(3, min(int(lanes) * 2, 10))
    except (TypeError, ValueError):
        return 4


def _link_tooltip(link) -> str:
    results = link.results or {}
    skdf = (link.metadata or {}).get("skdf") or {}
    values: dict[str, Any] = {
        "ID": link.id,
        "Name": link.name,
        "Length": f"{link.length_km} km",
        "LOS": results.get("LOS", "-"),
        "V/C": results.get("VC_ratio", "-"),
        "Hourly mode": results.get("hourly_mode", "-"),
        "SKDF AADT": skdf.get("traffic_aadt", skdf.get("traffic", "-")),
        "N_hour_avg": results.get("N_hour_avg", "-"),
        "N_hour_design": results.get("N_hour_design", "-"),
        "P_odm": results.get("P_odm", "-"),
        "Cars": (link.traffic_counts or {}).get("car", "-"),
    }
    if skdf:
        values["SKDF road"] = skdf.get("road_name", "-")
        values["SKDF score"] = skdf.get("match_score", "-")
        values["SKDF capacity"] = results.get(
            "capacity_skdf_reference",
            skdf.get("capacity_values", skdf.get("capacity_total", skdf.get("capacity", []))),
        )
    defaults_used = results.get("odm_defaults_used")
    if defaults_used:
        values["ODM defaults"] = ", ".join(defaults_used)
    return "<br>".join(f"<b>{key}:</b> {value}" for key, value in values.items())


def _add_skdf_layer(layer, csv_path: str, bounds: list[list[float]]) -> None:
    try:
        import folium
        from pyproj import Transformer
    except ImportError as exc:
        raise RuntimeError("Packages folium and pyproj are required for the SKDF overlay.") from exc

    from skdf_matcher import load_skdf_roads

    roads = load_skdf_roads(csv_path)
    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)

    for road in roads:
        for line in getattr(road.geometry, "geoms", []):
            points = []
            for x, y in list(line.coords):
                lon, lat = transformer.transform(x, y)
                points.append([lat, lon])
            if len(points) < 2:
                continue
            bounds.extend(points)
            folium.PolyLine(
                locations=points,
                color="#d81b60",
                weight=5,
                opacity=0.72,
                tooltip=_skdf_tooltip(road),
            ).add_to(layer)


def _skdf_tooltip(road) -> str:
    values: dict[str, Any] = {
        "SKDF road_id": road.road_id or "-",
        "Road": road.road_name or road.full_name or "-",
        "AADT": road.traffic_aadt if road.traffic_aadt is not None else "-",
        "Traffic raw": road.traffic_values or "-",
        "Capacity raw": road.capacity_values or "-",
        "Lanes raw": road.lanes_values or "-",
        "Speed raw": road.speed_limit_values or "-",
    }
    return "<br>".join(f"<b>{key}:</b> {value}" for key, value in values.items())


def _capacity(link) -> float | None:
    results = link.results or {}
    odm_capacity = _float_value(results.get("P_odm"))
    if odm_capacity is not None:
        return odm_capacity
    params = link.parameters or {}
    lanes = _float_value(params.get("lanes_total") or params.get("lanes_count") or 1)
    base = _float_value(params.get("capacity_per_lane_base") or params.get("saturation_flow_base") or 1800)
    if lanes is None or base is None:
        return None
    return lanes * base


def _float_value(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
