import requests

def get_roads_in_bbox(session: requests.Session, bbox, zoom=14, scale_factor=1):
    url = "https://xn--d1aluo.xn--p1ai/api-pg/rpc/get_road_lr_geobox"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Content-Profile": "gis_api_public",
        "Origin": "https://xn--d1aluo.xn--p1ai",
        "Referer": "https://xn--d1aluo.xn--p1ai/map",
        "User-Agent": "Mozilla/5.0",
    }
    payload = {
        "p_box": bbox,
        "p_scale_factor": scale_factor,
        "p_zoom": zoom,
    }

    r = session.post(url, json=payload, headers=headers, timeout=60)
    r.raise_for_status()
    return r.json()


def extract_roads(feature_collection: dict):
    rows = []

    for feature in feature_collection.get("features", []):
        props = feature.get("properties", {})
        rows.append({
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
            "geometry": (feature.get("geometry") or {}).get("coordinates"),
        })
    return rows


def get_passport_id(session: requests.Session, road_id: int, object_type: int = 4):
    url = "https://xn--d1aluo.xn--p1ai/api-pg/rpc/f_get_approved_passport_id_by_object"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Content-Profile": "query_api",
        "Origin": "https://xn--d1aluo.xn--p1ai",
        "Referer": "https://xn--d1aluo.xn--p1ai/map",
        "User-Agent": "Mozilla/5.0",
    }
    payload = {
        "object_id": road_id,
        "object_type": object_type
    }

    r = session.post(url, json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("passport_id")


def get_road_passport(session: requests.Session, passport_id: int):
    url = f"https://xn--d1aluo.xn--p1ai/api/v3/portal/hwm/passports/roads/{passport_id}"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://xn--d1aluo.xn--p1ai/passports/roads/{passport_id}",
        "User-Agent": "Mozilla/5.0",
    }

    r = session.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


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

def flatten_passport(passport_json: dict):
    d = passport_json["data"]
    return {
        "passport_id": d.get("passport_id"),
        "road_id": d.get("road_id"),
        "road_data_id": d.get("road_data_id"),
        "full_name": d.get("full_name"),
        "road_number_full": d.get("road_number_full"),
        "road_number_short": d.get("road_number_short"),
        "region": first_name(d.get("region")),
        "city": (d.get("fias_lvl_4") or {}).get("name"),
        "municipality": first_name(d.get("municipality_formation")),
        "owner": first_name(d.get("owner")),
        "category": first_name(d.get("category")),
        "class_of_road": first_name(d.get("class_of_road")),
        "length_km": d.get("length"),
        "start_passport": d.get("start"),
        "finish_passport": d.get("finish"),
        "traffic": d.get("traffic"),
        "traffic_1": safe_first(d.get("traffic")),
        "capacity": d.get("capacity"),
        "capacity_1": d.get("capacity")[0] if isinstance(d.get("capacity"), list) and len(d.get("capacity")) > 0 else None,
        "capacity_2": d.get("capacity")[1] if isinstance(d.get("capacity"), list) and len(d.get("capacity")) > 1 else None,
        "capacity_3": d.get("capacity")[2] if isinstance(d.get("capacity"), list) and len(d.get("capacity")) > 2 else None,
        "lanes": d.get("lanes"),
        "lanes_1": safe_first(d.get("lanes")),
        "speed_limit": d.get("speed_limit"),
        "speed_limit_1": safe_first(d.get("speed_limit")),
        "value_of_the_road_passport": first_name(d.get("value_of_the_road")),
    }


import time
import pandas as pd
import requests

# bbox = [9221169.672109539, 7358967.2403991, 9237488.977648424, 7380675.356432091]
# bbox = [9225362.998402964, 7364600.225959764, 9233599.088200692, 7375253.636777011]
# bbox = [9226459.795153216, 7365509.919379489, 9235164.06174919, 7376163.330196735]
bbox = [9226459.795153216,7365309.272180239,9235164.06174919,7375962.682997486]
with requests.Session() as session:
    fc = get_roads_in_bbox(session, bbox, zoom=14, scale_factor=1)
    roads = extract_roads(fc)

    # убираем дубли по road_id
    unique_roads = {}
    for r in roads:
        rid = r["road_id"]
        if rid is not None and rid not in unique_roads:
            unique_roads[rid] = r

    result_rows = []

    for road_id, road_row in unique_roads.items():
        try:
            passport_id = get_passport_id(session, road_id, object_type=4)
            if not passport_id:
                continue

            passport = get_road_passport(session, passport_id)
            passport_row = flatten_passport(passport)

            merged = {**road_row, **passport_row}
            result_rows.append(merged)

            # time.sleep(0.3)
        except Exception as e:
            print(f"Ошибка для road_id={road_id}: {e}")

df = pd.DataFrame(result_rows)
df.to_csv("nsk_roads_bbox_3.csv", index=False, encoding="utf-8-sig")
print(df.head())