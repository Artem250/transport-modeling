from __future__ import annotations

import argparse
import json
from html import escape
from pathlib import Path
from statistics import mean

from project_loader import ProjectLoader
from skdf_matcher import load_skdf_roads


def main() -> None:
    parser = argparse.ArgumentParser(description="Export an HTML map with SKDF roads over the OSM project graph.")
    parser.add_argument("--project", default="osm_network_project.json", help="Input project JSON.")
    parser.add_argument("--skdf-csv", default="nsk_roads_bbox.csv", help="SKDF CSV exported from api_test.py.")
    parser.add_argument("--output", default="skdf_osm_overlay_map.html", help="Output HTML path.")
    args = parser.parse_args()

    project = ProjectLoader().load(args.project)
    html = build_overlay_html(project, args.skdf_csv)
    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")
    print(f"Saved overlay map: {output_path.resolve()}")


def build_overlay_html(project, skdf_csv_path: str) -> str:
    from pyproj import Transformer

    osm_features = []
    skdf_features = []
    bounds = []

    ALLOWED_HIGHWAYS = {
        "primary",
        "secondary",
        "tertiary",
        "residential",
    }

    for link in project.network.links.values():
        highway = (link.metadata or {}).get("highway")
        if highway not in ALLOWED_HIGHWAYS:
            continue
        points = _project_link_points(link)
        if len(points) < 2:
            continue
        bounds.extend(points)
        osm_features.append(
            {
                "name": link.name,
                "color": _osm_link_color(link),
                "weight": _osm_link_weight(link),
                "points": points,
                "tooltip": {
                    "ID": link.id,
                    "Name": link.name,
                    "Length (km)": link.length_km,
                    "LOS": (link.results or {}).get("LOS", "-"),
                    "Hourly mode": (link.results or {}).get("hourly_mode", "-"),
                    "SKDF AADT": (((link.metadata or {}).get("skdf") or {}).get("traffic_aadt")
                                   or (((link.metadata or {}).get("skdf") or {}).get("traffic", "-"))),
                    "N_hour_avg": (link.results or {}).get("N_hour_avg", "-"),
                    "N_hour_design": (link.results or {}).get("N_hour_design", "-"),
                    "P_odm": (link.results or {}).get("P_odm", "-"),
                    "SKDF capacity": (link.results or {}).get(
                        "capacity_skdf_reference",
                        ((link.metadata or {}).get("skdf") or {}).get(
                            "capacity_values",
                            ((link.metadata or {}).get("skdf") or {}).get(
                                "capacity_total",
                                ((link.metadata or {}).get("skdf") or {}).get("capacity", []),
                            ),
                        ),
                    ),
                    "Cars": (link.traffic_counts or {}).get("car", "-"),
                },
            }
        )

    transformer = Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True)
    for road in load_skdf_roads(skdf_csv_path):
        for line in getattr(road.geometry, "geoms", []):
            points = []
            for x, y in list(line.coords):
                lon, lat = transformer.transform(x, y)
                points.append([lat, lon])
            if len(points) < 2:
                continue
            bounds.extend(points)
            skdf_features.append(
                {
                    "name": road.road_name or road.full_name or road.road_id,
                    "color": "#d81b60",
                    "weight": 5,
                    "points": points,
                    "tooltip": {
                        "SKDF road_id": road.road_id or "-",
                        "Road": road.road_name or road.full_name or "-",
                        "AADT": road.traffic_aadt if road.traffic_aadt is not None else "-",
                        "Traffic raw": road.traffic_values or "-",
                        "Capacity raw": road.capacity_values or "-",
                        "Lanes raw": road.lanes_values or "-",
                        "Speed raw": road.speed_limit_values or "-",
                    },
                }
            )

    center = _center(bounds)
    bounds_js = json.dumps(bounds, ensure_ascii=False)
    osm_js = json.dumps(osm_features, ensure_ascii=False)
    skdf_js = json.dumps(skdf_features, ensure_ascii=False)
    title = escape(project.project_name or "Overlay map")

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
      background: rgba(255,255,255,0.92);
      padding: 10px 12px;
      border-radius: 8px;
      box-shadow: 0 2px 10px rgba(0,0,0,0.15);
      line-height: 1.45;
      font: 13px/1.4 Arial, sans-serif;
    }}
    .legend-row {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
    }}
    .legend-line {{
      width: 24px;
      height: 0;
      border-top: 4px solid #000;
    }}
  </style>
</head>
<body>
  <div id="map"></div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
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

    const osmFeatures = {osm_js};
    const skdfFeatures = {skdf_js};
    const allBounds = {bounds_js};

    function tooltipHtml(values) {{
      return Object.entries(values)
        .map(([key, value]) => `<b>${{key}}:</b> ${{value}}`)
        .join('<br>');
    }}

    const osmLayer = L.layerGroup(
      osmFeatures.map(feature => L.polyline(feature.points, {{
        color: feature.color,
        weight: feature.weight,
        opacity: 0.85
      }}).bindTooltip(tooltipHtml(feature.tooltip)))
    ).addTo(map);

    const skdfLayer = L.layerGroup(
      skdfFeatures.map(feature => L.polyline(feature.points, {{
        color: feature.color,
        weight: feature.weight,
        opacity: 0.72
      }}).bindTooltip(tooltipHtml(feature.tooltip)))
    ).addTo(map);

    const baseLayers = {{
      "OSM": osmBase,
      "CartoDB Positron": cartoBase
    }};
    const overlays = {{
      "OSM graph": osmLayer,
      "SKDF roads": skdfLayer
    }};
    L.control.layers(baseLayers, overlays, {{ collapsed: false }}).addTo(map);

    if (allBounds.length > 0) {{
      map.fitBounds(allBounds, {{ padding: [20, 20] }});
    }} else {{
      map.setView({json.dumps(center)}, 13);
    }}

    const legend = L.control({{ position: 'topright' }});
    legend.onAdd = function() {{
      const div = L.DomUtil.create('div', 'legend');
      div.innerHTML = `
        <div><b>Слои</b></div>
        <div class="legend-row"><span class="legend-line" style="border-top-color:#1976d2"></span>OSM graph</div>
        <div class="legend-row"><span class="legend-line" style="border-top-color:#d81b60"></span>SKDF roads</div>
      `;
      return div;
    }};
    legend.addTo(map);
  </script>
</body>
</html>
"""


def _project_link_points(link) -> list[list[float]]:
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


def _osm_link_color(link) -> str:
    results = link.results or {}
    los = results.get("LOS")
    if los == "A":
        return "#1a9850"
    if los == "B":
        return "#66bd63"
    if los == "C":
        return "#fee08b"
    if los == "D":
        return "#fdae61"
    if los == "E":
        return "#f46d43"
    if los == "F":
        return "#d73027"
    return "#1976d2"


def _osm_link_weight(link) -> int:
    params = link.parameters or {}
    lanes = params.get("lanes_total") or params.get("lanes_count") or 1
    try:
        return max(3, min(int(lanes) * 2, 10))
    except (TypeError, ValueError):
        return 4


def _center(bounds: list[list[float]]) -> list[float]:
    if not bounds:
        return [54.841, 83.106]
    return [mean(point[0] for point in bounds), mean(point[1] for point in bounds)]


if __name__ == "__main__":
    main()
