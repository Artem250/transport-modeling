import json
from typing import Any

import pandas as pd
import requests


PUBLIC_HOST = "https://xn--d1aluo.xn--p1ai"
GEOSERVER_WFS_URL = f"{PUBLIC_HOST}/geoserver/skdf_open/wfs"
ROAD_CONDITIONS_LAYER = "skdf_open:lyr_road_conditions_traffic"


def get_roads_in_bbox(session: requests.Session, bbox, zoom=14, scale_factor=1):
    url = f"{PUBLIC_HOST}/api-pg/rpc/get_road_lr_geobox"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Content-Profile": "gis_api_public",
        "Origin": PUBLIC_HOST,
        "Referer": f"{PUBLIC_HOST}/map",
        "User-Agent": "Mozilla/5.0",
    }
    payload = {
        "p_box": bbox,
        "p_scale_factor": scale_factor,
        "p_zoom": zoom,
    }

    response = session.post(url, json=payload, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def extract_roads(feature_collection: dict[str, Any]):
    rows = []

    for feature in feature_collection.get("features", []):
        props = feature.get("properties", {})
        rows.append(
            {
                "feature_id": feature.get("id"),
                "road_id": props.get("road_id"),
                "road_part_id": props.get("road_part_id"),
                "road_name": props.get("road_name"),
                "start_km": props.get("start_km"),
                "finish_km": props.get("finish_km"),
                "road_length": props.get("road_length"),
                "geom_length": props.get("geom_length"),
                "value_of_the_road": props.get("value_of_the_road"),
                "value_of_the_road_gid": props.get("value_of_the_road_gid"),
                "geometry_type": (feature.get("geometry") or {}).get("type"),
                "geometry": json.dumps(
                    (feature.get("geometry") or {}).get("coordinates"),
                    ensure_ascii=False,
                ),
            }
        )
    return rows


def get_passport_id(session: requests.Session, road_id: int, object_type: int = 4):
    url = f"{PUBLIC_HOST}/api-pg/rpc/f_get_approved_passport_id_by_object"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Content-Profile": "query_api",
        "Origin": PUBLIC_HOST,
        "Referer": f"{PUBLIC_HOST}/map",
        "User-Agent": "Mozilla/5.0",
    }
    payload = {
        "object_id": road_id,
        "object_type": object_type,
    }

    response = session.post(url, json=payload, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json().get("passport_id")


def get_road_passport(session: requests.Session, passport_id: int):
    url = f"{PUBLIC_HOST}/api/v3/portal/hwm/passports/roads/{passport_id}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{PUBLIC_HOST}/passports/roads/{passport_id}",
        "User-Agent": "Mozilla/5.0",
    }

    response = session.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def get_road_condition_segments(
    session: requests.Session,
    road_id: int,
    layer_name: str = ROAD_CONDITIONS_LAYER,
    count: int = 5000,
):
    """
    Segment-level traffic/capacity/speed data lives in geoserver layers
    lyr_road_conditions_*. The traffic layer already exposes all fields we
    need, so one WFS query per road is enough.
    """
    params = {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer_name,
        "outputFormat": "json",
        "CQL_FILTER": f"road_id={road_id}",
        "count": str(count),
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": PUBLIC_HOST,
        "Referer": f"{PUBLIC_HOST}/map",
        "User-Agent": "Mozilla/5.0",
    }

    response = session.get(GEOSERVER_WFS_URL, params=params, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def first_name(value):
    if isinstance(value, list) and value:
        return value[0].get("name")
    if isinstance(value, dict):
        return value.get("name")
    return None


def safe_first(lst):
    if isinstance(lst, list) and lst:
        return lst[0]
    return None


def flatten_passport(passport_json: dict[str, Any]):
    data = passport_json["data"]
    if first_name(data.get("value_of_the_road")) != "Автомобильные дороги местного значения":
        return None
    return {
        "passport_id": data.get("passport_id"),
        "road_id": data.get("road_id"),
        "road_data_id": data.get("road_data_id"),
        "full_name": data.get("full_name"),
        "road_number_full": data.get("road_number_full"),
        "road_number_short": data.get("road_number_short"),
        "region": first_name(data.get("region")),
        "city": (data.get("fias_lvl_4") or {}).get("name"),
        "municipality": first_name(data.get("municipality_formation")),
        "owner": first_name(data.get("owner")),
        "category": first_name(data.get("category")),
        "class_of_road": first_name(data.get("class_of_road")),
        "length_km": data.get("length"),
        "start_passport": data.get("start"),
        "finish_passport": data.get("finish"),
        "traffic": data.get("traffic"),
        "traffic_1": safe_first(data.get("traffic")),
        "capacity": data.get("capacity"),
        "capacity_1": data.get("capacity")[0]
        if isinstance(data.get("capacity"), list) and len(data.get("capacity")) > 0
        else None,
        "capacity_2": data.get("capacity")[1]
        if isinstance(data.get("capacity"), list) and len(data.get("capacity")) > 1
        else None,
        "capacity_3": data.get("capacity")[2]
        if isinstance(data.get("capacity"), list) and len(data.get("capacity")) > 2
        else None,
        "lanes": data.get("lanes"),
        "lanes_1": safe_first(data.get("lanes")),
        "speed_limit": data.get("speed_limit"),
        "speed_limit_1": safe_first(data.get("speed_limit")),
        "value_of_the_road_passport": first_name(data.get("value_of_the_road")),
    }


def extract_road_condition_segments(feature_collection: dict[str, Any]):
    rows = []

    for feature in feature_collection.get("features", []):
        props = feature.get("properties", {})
        bbox = feature.get("bbox") or [None, None, None, None]
        rows.append(
            {
                "segment_feature_id": feature.get("id"),
                "segment_object_id": props.get("id"),
                "road_id": props.get("road_id"),
                "road_name_segment": props.get("road_name"),
                "road_part_id_segment": props.get("part_id"),
                "start_km_segment": props.get("start_km"),
                "finish_km_segment": props.get("finish_km"),
                "traffic_segment": props.get("traffic"),
                "capacity_segment": props.get("capacity"),
                "top_speed_segment": props.get("top_speed"),
                "lanes_segment": props.get("lanes"),
                "roadway_width_segment": props.get("roadway_width"),
                "roadbed_width_segment": props.get("roadbed_width"),
                "loading_segment": props.get("loading"),
                "os_segment": props.get("os"),
                "settlement_segment": props.get("settlment_name"),
                "geometry_type_segment": (feature.get("geometry") or {}).get("type"),
                "geometry_segment": json.dumps(
                    (feature.get("geometry") or {}).get("coordinates"),
                    ensure_ascii=False,
                ),
                "bbox_minx_segment": bbox[0],
                "bbox_miny_segment": bbox[1],
                "bbox_maxx_segment": bbox[2],
                "bbox_maxy_segment": bbox[3],
            }
        )
    return rows


# bbox = [9221169.672109539, 7358967.2403991, 9237488.977648424, 7380675.356432091]
# bbox = [9225362.998402964, 7364600.225959764, 9233599.088200692, 7375253.636777011]
# bbox = [9226459.795153216, 7365509.919379489, 9235164.06174919, 7376163.330196735]
bbox = [9226459.795153216, 7365309.272180239, 9235164.06174919, 7375962.682997486]

with requests.Session() as session:
    feature_collection = get_roads_in_bbox(session, bbox, zoom=14, scale_factor=1)
    roads = extract_roads(feature_collection)

    unique_roads = {}
    for road in roads:
        road_id = road["road_id"]
        if road_id is not None and road_id not in unique_roads:
            unique_roads[road_id] = road

    road_rows = []
    segment_rows = []

    for road_id, road_row in unique_roads.items():
        try:
            passport_id = get_passport_id(session, road_id, object_type=4)
            if not passport_id:
                continue

            passport = get_road_passport(session, passport_id)
            passport_row = flatten_passport(passport)

            # 1. Проверяем фильтр
            if passport_row is not None:
                # 2. Только если фильтр пройден, создаем merged_road
                merged_road = {**road_row, **passport_row}
                road_rows.append(merged_road)

                # 3. ПЕРЕНОСИМ сюда получение и обработку сегментов
                segments_fc = get_road_condition_segments(session, road_id)
                segments = extract_road_condition_segments(segments_fc)
                for segment in segments:
                    segment_rows.append({**merged_road, **segment})
            else:
                # Если паспорт не прошел фильтр, мы просто идем к следующей итерации
                continue

        except Exception as exc:
            print(f"Error for road_id={road_id}: {exc}")

road_df = pd.DataFrame(road_rows)
road_df.to_csv("nsk_roads_bbox_3_2.csv", index=False, encoding="utf-8-sig")

segment_df = pd.DataFrame(segment_rows)
segment_df.to_csv("nsk_roads_bbox_3_segments_2.csv", index=False, encoding="utf-8-sig")

print(road_df.head())
print(segment_df.head())
