from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path
from statistics import mean
from typing import Any

from project_loader import ProjectLoader


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an HTML map for SKDF segment traffic and capacity.")
    parser.add_argument("--project", default="skdf_segments_project.json", help="Project JSON generated from SKDF segments.")
    parser.add_argument("--output", default="skdf_segments_map.html", help="Output HTML path.")
    args = parser.parse_args()

    project = ProjectLoader().load(args.project)
    html = build_skdf_segment_map_html(project)
    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"Saved SKDF segment map: {output_path.resolve()}")


def build_skdf_segment_map_html(project) -> str:
    features = []
    bounds = []
    boundary_points: dict[str, dict[str, Any]] = {}

    for link in project.network.links.values():
        points = _link_points(link)
        if len(points) < 2:
            continue
        bounds.extend(points)
        _add_boundary_point(boundary_points, points[0])
        _add_boundary_point(boundary_points, points[-1])
        results = link.results or {}
        skdf = (link.metadata or {}).get("skdf") or {}
        intensity = _first_present(results.get("V"), skdf.get("traffic"), (link.traffic_counts or {}).get("car"))
        capacity = _first_present(results.get("C_initial"), skdf.get("capacity_total"), (link.parameters or {}).get("capacity_total_skdf"))
        vc_ratio = _first_present(results.get("VC_ratio"), _ratio(intensity, capacity))
        features.append(
            {
                "id": link.id,
                "name": link.name,
                "points": points,
                "midpoint": points[len(points) // 2],
                "color": _vc_color(vc_ratio),
                "weight": _weight(link),
                "label": f"I:{_fmt(intensity)} C:{_fmt(capacity)}",
                "tooltip": {
                    "ID": link.id,
                    "Road": skdf.get("road_name") or skdf.get("full_name") or link.name,
                    "Segment": skdf.get("segment_object_id") or "-",
                    "km": f"{_fmt(skdf.get('start_km'))} - {_fmt(skdf.get('finish_km'))}",
                    "Intensity": _fmt(intensity),
                    "Capacity": _fmt(capacity),
                    "V/C": _fmt(vc_ratio),
                    "LOS": results.get("LOS", "-"),
                    "Lanes": _fmt(skdf.get("lanes")),
                    "Speed": _fmt(skdf.get("speed_limit")),
                    "Length km": _fmt(link.length_km),
                },
            }
        )

    center = _center(bounds)
    title = escape(project.project_name or "SKDF segment map")
    features_js = json.dumps(features, ensure_ascii=False)
    boundary_points_js = json.dumps(list(boundary_points.values()), ensure_ascii=False)
    bounds_js = json.dumps(bounds, ensure_ascii=False)
    center_js = json.dumps(center, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    html, body, #map {{
      height: 100%;
      margin: 0;
    }}
    .legend {{
      background: rgba(255,255,255,0.94);
      padding: 10px 12px;
      border-radius: 6px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.16);
      font: 13px/1.4 Arial, sans-serif;
    }}
    .legend-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
    }}
    .legend-line {{
      width: 28px;
      border-top: 5px solid #000;
    }}
    .skdf-label {{
      padding: 1px 4px;
      border: 1px solid rgba(0,0,0,0.35);
      border-radius: 3px;
      background: rgba(255,255,255,0.86);
      color: #111;
      font: 10px/1.2 Arial, sans-serif;
      white-space: nowrap;
      pointer-events: none;
    }}
    .leaflet-interactive.segment-boundary {{
      filter: drop-shadow(0 1px 1px rgba(0,0,0,0.35));
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const features = {features_js};
    const boundaryPoints = {boundary_points_js};
    const allBounds = {bounds_js};
    const map = L.map('map', {{ preferCanvas: true }});
    const osmBase = L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
      maxZoom: 19,
      attribution: '&copy; OpenStreetMap contributors'
    }}).addTo(map);
    const cartoBase = L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
      subdomains: 'abcd',
      maxZoom: 20,
      attribution: '&copy; OpenStreetMap contributors &copy; CARTO'
    }});

    function tooltipHtml(values) {{
      return Object.entries(values)
        .map(([key, value]) => `<b>${{key}}:</b> ${{value}}`)
        .join('<br>');
    }}

    const segmentHaloLayer = L.layerGroup(
      features.map(feature => L.polyline(feature.points, {{
        color: '#263238',
        weight: feature.weight + 3,
        opacity: 0.72
      }}))
    ).addTo(map);

    const segmentsLayer = L.layerGroup(
      features.map(feature => {{
        const line = L.polyline(feature.points, {{
          color: feature.color,
          weight: feature.weight,
          opacity: 0.92
        }}).bindTooltip(tooltipHtml(feature.tooltip), {{ sticky: true }});
        line.on('mouseover', () => line.setStyle({{ weight: feature.weight + 3, opacity: 1 }}));
        line.on('mouseout', () => line.setStyle({{ weight: feature.weight, opacity: 0.92 }}));
        return line;
      }})
    ).addTo(map);

    const boundaryLayer = L.layerGroup(
      boundaryPoints.map(point => L.circleMarker(point.location, {{
        className: 'segment-boundary',
        radius: point.count > 1 ? 4 : 3,
        color: '#111827',
        weight: 1.5,
        fill: true,
        fillColor: '#ffffff',
        fillOpacity: 0.95
      }}).bindTooltip(`Segment boundary<br>${{point.count}} endpoint(s)`, {{ sticky: true }}))
    ).addTo(map);

    const labelsLayer = L.layerGroup(
      features.map(feature => L.marker(feature.midpoint, {{
        interactive: false,
        icon: L.divIcon({{
          className: '',
          html: `<div class="skdf-label">${{feature.label}}</div>`,
          iconSize: null
        }})
      }}))
    );

    L.control.layers(
      {{ "OSM": osmBase, "CartoDB Positron": cartoBase }},
      {{ "SKDF segment halo": segmentHaloLayer, "SKDF segments": segmentsLayer, "Segment boundaries": boundaryLayer, "I/C labels": labelsLayer }},
      {{ collapsed: false }}
    ).addTo(map);

    if (allBounds.length > 0) {{
      map.fitBounds(allBounds, {{ padding: [18, 18] }});
    }} else {{
      map.setView({center_js}, 13);
    }}

    const legend = L.control({{ position: 'topright' }});
    legend.onAdd = function() {{
      const div = L.DomUtil.create('div', 'legend');
      div.innerHTML = `
        <div><b>V/C</b></div>
        <div class="legend-row"><span class="legend-line" style="border-top-color:#1a9850"></span>≤ 0.35</div>
        <div class="legend-row"><span class="legend-line" style="border-top-color:#66bd63"></span>0.35-0.54</div>
        <div class="legend-row"><span class="legend-line" style="border-top-color:#fee08b"></span>0.54-0.77</div>
        <div class="legend-row"><span class="legend-line" style="border-top-color:#fdae61"></span>0.77-0.93</div>
        <div class="legend-row"><span class="legend-line" style="border-top-color:#d73027"></span>> 1.00</div>
        <div class="legend-row"><span style="display:inline-block;width:8px;height:8px;border:2px solid #111827;border-radius:50%;background:#fff"></span>segment boundary</div>
      `;
      return div;
    }};
    legend.addTo(map);
  </script>
</body>
</html>
"""


def _add_boundary_point(boundary_points: dict[str, dict[str, Any]], point: list[float]) -> None:
    key = f"{round(point[0], 7)},{round(point[1], 7)}"
    if key not in boundary_points:
        boundary_points[key] = {"location": [round(point[0], 7), round(point[1], 7)], "count": 0}
    boundary_points[key]["count"] += 1


def _link_points(link) -> list[list[float]]:
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


def _weight(link) -> int:
    lanes = _number((link.parameters or {}).get("lanes_total")) or 1
    return max(3, min(int(round(lanes)) * 2, 9))


def _vc_color(vc_ratio: Any) -> str:
    value = _number(vc_ratio)
    if value is None:
        return "#1976d2"
    if value > 1.0:
        return "#d73027"
    if value > 0.93:
        return "#f46d43"
    if value > 0.77:
        return "#fdae61"
    if value > 0.54:
        return "#fee08b"
    if value > 0.35:
        return "#66bd63"
    return "#1a9850"


def _ratio(numerator: Any, denominator: Any) -> float | None:
    n = _number(numerator)
    d = _number(denominator)
    if n is None or d is None or d <= 0:
        return None
    return n / d


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "-"
    if number.is_integer():
        return str(int(number))
    return f"{number:.3f}".rstrip("0").rstrip(".")


def _center(bounds: list[list[float]]) -> list[float]:
    if not bounds:
        return [54.841, 83.106]
    return [mean(point[0] for point in bounds), mean(point[1] for point in bounds)]


if __name__ == "__main__":
    main()
