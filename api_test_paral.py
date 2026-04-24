import time
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed


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

    r = session.post(url, json=payload, headers=headers, timeout=20)
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

    r = session.get(url, headers=headers, timeout=20)
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
    capacity = d.get("capacity")
    return {
        "passport_id": d.get("passport_id"),
        "road_id_passport": d.get("road_id"),
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
        "capacity": capacity,
        "capacity_1": capacity[0] if isinstance(capacity, list) and len(capacity) > 0 else None,
        "capacity_2": capacity[1] if isinstance(capacity, list) and len(capacity) > 1 else None,
        "capacity_3": capacity[2] if isinstance(capacity, list) and len(capacity) > 2 else None,
        "lanes": d.get("lanes"),
        "lanes_1": safe_first(d.get("lanes")),
        "speed_limit": d.get("speed_limit"),
        "speed_limit_1": safe_first(d.get("speed_limit")),
        "value_of_the_road_passport": first_name(d.get("value_of_the_road")),
    }


def shrink_geometry(coords, max_points=80):
    """
    Грубое прореживание координат для CSV.
    Не для GIS-анализа, только чтобы поле не раздувалось.
    """
    if not isinstance(coords, list):
        return coords

    flat = []
    for part in coords:
        if isinstance(part, list):
            flat.extend(part)

    if len(flat) <= max_points:
        return coords

    step = max(1, len(flat) // max_points)
    sampled = flat[::step]
    if sampled[-1] != flat[-1]:
        sampled.append(flat[-1])

    return [sampled]


def process_one_road(road_row: dict):
    road_id = road_row["road_id"]

    with requests.Session() as session:
        passport_id = get_passport_id(session, road_id, object_type=4)
        if not passport_id:
            return {
                **road_row,
                "status": "passport_not_found"
            }

        passport = get_road_passport(session, passport_id)
        passport_row = flatten_passport(passport)

        return {
            **road_row,
            **passport_row,
            "status": "ok"
        }


if __name__ == "__main__":
    bbox = [9226459.795153216, 7365309.272180239, 9235164.06174919, 7375962.682997486]

    t0 = time.time()

    with requests.Session() as session:
        fc = get_roads_in_bbox(session, bbox, zoom=14, scale_factor=1)
        roads = extract_roads(fc)

    unique_roads = {}
    for r in roads:
        rid = r["road_id"]
        if rid is not None and rid not in unique_roads:
            # обрезаем слишком тяжёлую геометрию для CSV
            r["geometry"] = shrink_geometry(r["geometry"], max_points=80)
            unique_roads[rid] = r

    result_rows = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(process_one_road, row) for row in unique_roads.values()]

        for future in as_completed(futures):
            try:
                result_rows.append(future.result())
            except Exception as e:
                result_rows.append({
                    "status": "error",
                    "error_message": str(e)
                })

    df = pd.DataFrame(result_rows)
    df.to_csv("nsk_roads_bbox_fast.csv", index=False, encoding="utf-8-sig")

    print(f"Готово. Строк: {len(df)}")
    print(f"Время: {time.time() - t0:.2f} сек")