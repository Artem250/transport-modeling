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
    def __init__(self, project, title="Traffic map"):
        super().__init__()
        self.setWindowTitle(title)
        self.resize(1200, 800)

        configure_webengine_profile()
        self.browser = QWebEngineView()
        self.browser.setHtml(build_project_map_html(project))

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


def build_project_map_html(project) -> str:
    try:
        import folium
    except ImportError as exc:
        raise RuntimeError(
            "Для web-карты нужен пакет folium в том же Python, которым запущено приложение.\n"
            f"Текущий Python: {sys.executable}\n"
            f"Команда установки: \"{sys.executable}\" -m pip install folium"
        ) from exc

    center = _project_center(project)
    m = folium.Map(
        location=center,
        zoom_start=14,
        tiles="CartoDB positron",
    )

    bounds = []
    for link in project.network.links.values():
        points = _link_latlon_points(link)
        if len(points) < 2:
            continue
        bounds.extend(points)
        tooltip = _link_tooltip(link)
        folium.PolyLine(
            locations=points,
            color=_link_color(link),
            weight=_link_weight(link),
            opacity=0.85,
            tooltip=tooltip,
        ).add_to(m)

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
        ).add_to(m)

    if bounds:
        m.fit_bounds(bounds, padding=(20, 20))

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
    values: dict[str, Any] = {
        "ID": link.id,
        "Name": link.name,
        "Length": f"{link.length_km} km",
        "LOS": results.get("LOS", "-"),
        "V/C": results.get("VC_ratio", "-"),
        "Cars": (link.traffic_counts or {}).get("car", "-"),
    }
    return "<br>".join(f"<b>{key}:</b> {value}" for key, value in values.items())


def _capacity(link) -> float | None:
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
