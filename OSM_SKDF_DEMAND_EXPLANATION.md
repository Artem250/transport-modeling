# Разбор OSM, SKDF и demand-модулей

Документ разбирает актуальные файлы:

- `osm_project_importer.py`
- `traffic_viz.py`
- `skdf_matcher.py`
- `enrich_project_with_skdf.py`
- `demand_assignment_service.py`
- `demand_model_utils.py`
- `demand_model_wizard.py`

Также отдельно описаны связи с другими файлами проекта: `models.py`, `project_loader.py`, `project_saver.py`, `analysis_service.py`, `routing_service.py`, `validation_service.py`, `network_editor.py`, `folium_map_viewer.py`.

Важно: часть русских строк в исходниках отображается как битая кодировка. В этом документе смысл описан нормальным русским текстом, но фрагменты кода оставлены близко к исходнику.

## 1. Общая архитектура

Эти файлы образуют три связанных пайплайна.

### OSM-пайплайн

```text
OSM XML / osmnx graph
  -> osm_project_importer.py
  -> Project / Network / Node / Link
  -> ProjectSaver
  -> JSON проекта
```

Назначение: превратить карту OSM в транспортный граф проекта.

### SKDF-пайплайн

```text
Project JSON + SKDF CSV
  -> enrich_project_with_skdf.py
  -> skdf_matcher.py
  -> link.traffic_counts / link.parameters / link.metadata["skdf"]
  -> ProjectSaver
  -> enriched Project JSON
```

Назначение: сопоставить OSM-links с официальными/внешними дорожными данными СКДФ и обогатить граф трафиком, пропускной способностью, полосами и ограничением скорости.

### Demand-пайплайн

```text
Project
  -> demand_model_wizard.py
  -> project.demand_model
  -> DemandAssignmentService
  -> link.traffic_counts
  -> AnalysisService
  -> link.results / route results
```

Назначение: создать или применить модель спроса. Demand-модель перераспределяет потоки по маршрутам и затем запускает обычный расчет V/C, LOS и задержек.

## 2. Связи между файлами

```text
osm_project_importer.py
  -> models.py
  -> project_saver.py только в CLI main()

skdf_matcher.py
  -> models.py
  -> shapely
  -> pyproj

enrich_project_with_skdf.py
  -> project_loader.py
  -> skdf_matcher.py
  -> project_saver.py

demand_model_utils.py
  -> models.py

demand_assignment_service.py
  -> demand_model_utils.py
  -> models.py

demand_model_wizard.py
  -> models.py
  -> project_loader.py
  -> project_saver.py

traffic_viz.py
  -> analysis_service.py
  -> project_loader.py
  -> project_saver.py
  -> routing_service.py
  -> demand_model_wizard.py динамически
  -> network_editor.py динамически
  -> folium_map_viewer.py динамически
```

Связи с файлами вне текущего разбора:

- `models.py` теперь содержит `Project.demand_model`, а `Route` содержит `origin_node_id`, `destination_node_id`, `demand_value`, `vehicle_type`, `metadata`.
- `project_loader.py` загружает `demand_model` и нормализует demand-поля.
- `project_saver.py` сохраняет `demand_model`, но при активной demand-модели не сохраняет временно назначенные `assigned_demand` как исходные наблюдаемые потоки.
- `analysis_service.py` вызывает `DemandAssignmentService`, если у проекта есть `demand_model`.
- `routing_service.py` используется визуализатором для ручного поиска маршрута.
- `validation_service.py` используется `AnalysisService` перед demand assignment.

## 3. `osm_project_importer.py`

Файл импортирует дорожную сеть из OSM в объект `Project`.

### Константы

```python
ALLOWED_HIGHWAYS = {"primary", "secondary", "tertiary", "trunk"} #, "residential"}
ANGLE_TOLERANCE_DEG = 3.0
DISTANCE_TOLERANCE_M = 1.5
EARTH_RADIUS_M = 6371008.8
```

Что происходит:

- `ALLOWED_HIGHWAYS` ограничивает, какие OSM-дороги попадут в граф. Сейчас residential отключены.
- `ANGLE_TOLERANCE_DEG` и `DISTANCE_TOLERANCE_M` используются для упрощения геометрии: почти прямые промежуточные точки удаляются.
- `EARTH_RADIUS_M` используется для локальных метрических расчетов.

Практический эффект: импорт не берет все дороги подряд, а строит более компактный граф по важным типам дорог.

### `OsmImportError`

```python
class OsmImportError(RuntimeError):
    pass
```

Это специальная ошибка для OSM-импорта. Например, если отсутствует `osmnx`.

### `build_project_from_osm_point`

```python
def build_project_from_osm_point(
    location_point: tuple[float, float],
    dist_m: int = 1500,
    default_intensity: int = 600,
) -> Project:
    try:
        import osmnx as ox
    except ImportError as exc:
        raise OsmImportError(...) from exc

    graph = ox.graph_from_point(location_point, dist=dist_m, network_type="drive", simplify=True)
    return build_project_from_osmnx_graph(graph, default_intensity)
```

Что происходит:

- Функция скачивает граф OSM вокруг точки.
- `network_type="drive"` ограничивает граф автомобильной сетью.
- `simplify=True` просит osmnx упростить граф.
- После скачивания граф передается в `build_project_from_osmnx_graph`.

Связь:

- Эту функцию вызывает редактор сети при импорте участка карты.

### `build_project_from_osm_xml`

```python
tree = ET.parse(path)
root = tree.getroot()

osm_nodes = {
    node.get("id"): (float(node.get("lon")), float(node.get("lat")))
    for node in root.findall(".//node")
    if node.get("id") and node.get("lon") and node.get("lat")
}
```

Что происходит:

- XML-файл парсится через `ElementTree`.
- Все OSM-узлы складываются в словарь: `osm_id -> (lon, lat)`.

```python
highway_ways = []
for way in root.findall(".//way"):
    tags = {tag.get("k"): tag.get("v") for tag in way.findall("tag") if tag.get("k")}
    highway = tags.get("highway")
    if not highway or highway not in ALLOWED_HIGHWAYS:
        continue
    refs = [nd.get("ref") for nd in way.findall("nd") if nd.get("ref") in osm_nodes]
    if len(refs) > 1:
        highway_ways.append({"refs": refs, "tags": tags, "osm_id": way.get("id")})
```

Что происходит:

- Из OSM берутся только ways с подходящим `highway`.
- Для каждого way сохраняются ссылки на узлы, теги и `osm_id`.

```python
anchor_refs = _xml_anchor_refs(highway_ways, osm_nodes)
...
for refs in _split_refs_at_anchors(way["refs"], anchor_refs):
    points = [osm_nodes[ref] for ref in refs]
    if len(points) < 2 or _is_zero_length_polyline(points):
        continue
```

Что происходит:

- `anchor_refs` - точки, в которых way нужно разрезать.
- Разрез идет не по каждому OSM-сегменту, а по значимым точкам: начало/конец, пересечения, повороты.
- Нулевые линии отбрасываются.

```python
link_index = _add_segment_link(
    network,
    link_index,
    start_node_id,
    end_node_id,
    points,
    tags,
    default_intensity,
    {
        "source": "osm_xml",
        "osm_way_id": way["osm_id"],
        "osm_start_ref": start_ref,
        "osm_end_ref": end_ref,
    },
)
```

Что происходит:

- Каждый полученный кусок way становится `Link`.
- В `metadata` сохраняется связь с исходным OSM way и start/end refs.

```python
_merge_degree_two_continuations(network)
return Project(...)
```

Что происходит:

- После первичного построения граф упрощается: degree-two узлы на прямых продолжениях сливаются.
- Возвращается готовый `Project`.

### `build_project_from_osmnx_graph`

```python
for osm_id, data in graph.nodes(data=True):
    lon = data.get("x")
    lat = data.get("y")
    ...
    network.add_node(Node(...))
```

Что происходит:

- Узлы osmnx-графа превращаются в `Node`.
- `x` в osmnx означает longitude, `y` означает latitude.

```python
for u, v, key, data in graph.edges(keys=True, data=True):
    points = _edge_points(data, network.nodes[start_node_id], network.nodes[end_node_id])
    if len(points) < 2:
        continue

    link_index = _add_segment_link(...)
```

Что происходит:

- Ребра графа превращаются в `Link`.
- Геометрия берется из edge geometry, если она есть, иначе используется прямая между start/end node.

### `_xml_anchor_refs`

```python
for way in highway_ways:
    refs = way["refs"]
    anchor_refs.add(refs[0])
    anchor_refs.add(refs[-1])
    for ref in refs:
        ref_counts[ref] = ref_counts.get(ref, 0) + 1

for ref, count in ref_counts.items():
    if count > 1:
        anchor_refs.add(ref)
```

Что происходит:

- Начало и конец каждого way всегда становятся anchor-точками.
- Узел, который встречается в нескольких ways, тоже anchor: это вероятное пересечение.

```python
for previous_ref, current_ref, next_ref in zip(refs, refs[1:], refs[2:]):
    if not _is_redundant_geometry_point(previous_point, current_point, next_point):
        anchor_refs.add(current_ref)
```

Что происходит:

- Если промежуточная точка не является почти прямой, она сохраняется как anchor.
- Так граф сохраняет существенные повороты.

### `_split_refs_at_anchors`

```python
for index in range(1, len(refs)):
    if index == len(refs) - 1 or refs[index] in anchor_refs:
        chunk = refs[start_index : index + 1]
        if len(chunk) >= 2:
            chunks.append(chunk)
        start_index = index
```

Что происходит:

- Way разбивается на куски между anchor-точками.
- Каждый кусок потом превращается в отдельный `Link`.

### `_add_segment_link`

```python
lanes = _parse_lanes(tags.get("lanes"))
length_km = _polyline_length_km(points)
geometry_points = _simplify_hidden_geometry_points(points)
link_id = f"L{link_index}"
```

Что происходит:

- Из тегов читается число полос.
- Длина считается по геометрии.
- Геометрия упрощается.
- Link получает id `L1`, `L2`, ...

```python
network.add_link(
    Link(
        id=link_id,
        name=_text_value(tags.get("name"), f"OSM road {link_index}"),
        start_node_id=start_node_id,
        end_node_id=end_node_id,
        link_type="straight",
        length_km=round(length_km, 4),
        traffic_counts={"car": default_intensity},
        coords={
            "type": "polyline",
            "points": [[round(lon, 6), round(lat, 6)] for lon, lat in geometry_points],
            ...
        },
        parameters={
            "lanes_total": lanes,
            "capacity_per_lane_base": 1800,
            ...
        },
        metadata={...},
    )
)
```

Что происходит:

- OSM-сегмент становится `models.Link`.
- По умолчанию это `straight`.
- Начальный поток задается `default_intensity`.
- `coords` хранит полилинию и start/end координаты.
- `parameters` сразу готовятся под `AnalysisService`.

### `_merge_degree_two_continuations`

```python
for node_id in list(network.nodes):
    incident_links = [
        link
        for link in network.links.values()
        if link.start_node_id == node_id or link.end_node_id == node_id
    ]
    if len(incident_links) != 2:
        continue
```

Что происходит:

- Ищутся узлы степени 2.
- Такие узлы часто являются техническими точками на одной дороге.

```python
if not _links_can_merge(first_link, second_link):
    continue
if not _is_straight_continuation(first_link, second_link, node_id):
    continue

merged_link = _merged_link_through_node(first_link, second_link, node_id)
del network.links[first_link.id]
del network.links[second_link.id]
network.links[merged_link.id] = merged_link
del network.nodes[node_id]
```

Что происходит:

- Links сливаются только если это безопасно:
  - нет `disabled`;
  - совпадает source;
  - совпадают traffic_counts;
  - геометрия выглядит как прямое продолжение.
- Старые links и промежуточный node удаляются.
- Новый link сохраняет id первого link.

### Геометрическое упрощение

```python
def _is_redundant_geometry_point(previous_point, current_point, next_point) -> bool:
    if previous_point == current_point or current_point == next_point:
        return True

    angle = _turn_angle_deg(previous_point, current_point, next_point)
    if abs(180.0 - angle) > ANGLE_TOLERANCE_DEG:
        return False

    return _point_to_segment_distance_m(current_point, previous_point, next_point) <= DISTANCE_TOLERANCE_M
```

Что происходит:

- Точка считается лишней, если она почти лежит на прямой между соседними точками.
- Проверяются два условия:
  - угол почти 180 градусов;
  - расстояние до сегмента меньше допуска.

### CLI `main`

```python
parser.add_argument("--input", default="map_bez.osm")
parser.add_argument("--output", default="osm_network_project_map_bez.json")
parser.add_argument("--intensity", type=int, default=600)
...
project = build_project_from_osm_xml(args.input, args.intensity)
ProjectSaver().save(project, args.output)
```

Что происходит:

- Можно запустить импорт из командной строки.
- На выходе получается JSON-проект.

## 4. `skdf_matcher.py`

Файл сопоставляет links проекта с дорогами СКДФ и переносит данные СКДФ в проект.

### Dataclass-конфигурация

```python
@dataclass(frozen=True)
class SkdfMatchConfig:
    max_distance_m: float = 35.0
    buffer_m: float = 25.0
    min_overlap_ratio: float = 0.45
    min_score: float = 0.55
    name_bonus: float = 0.25
    name_mismatch_penalty: float = 0.20
    reject_named_mismatches: bool = True
    allow_strong_geometry_name_override: bool = False
    strong_geometry_distance_m: float = 5.0
    strong_geometry_overlap_ratio: float = 0.85
    way_group_enabled: bool = True
    way_group_max_distance_m: float = 60.0
```

Что происходит:

- Конфигурация задает правила сопоставления OSM link с SKDF road.
- Основные критерии:
  - максимальная дистанция;
  - доля overlap;
  - итоговый score;
  - бонус/штраф за совпадение названий;
  - разрешение на override при очень сильной геометрии.

### `SkdfRoad`

```python
@dataclass(frozen=True)
class SkdfRoad:
    row_index: int
    road_id: str
    road_part_id: str
    road_name: str
    full_name: str
    traffic: float | None
    capacity: float | None
    lanes: int | None
    speed_limit: float | None
    geometry: Any
    normalized_name: str
```

Что происходит:

- Одна строка CSV СКДФ превращается в объект.
- `geometry` - shapely `MultiLineString`.
- `normalized_name` нужен для сравнения названий.

### `LinkMatch`

```python
@dataclass(frozen=True)
class LinkMatch:
    link_id: str
    road: SkdfRoad
    score: float
    distance_m: float
    overlap_ratio: float
    name_similarity: float
    source: str = "direct"
```

Что происходит:

- Это результат сопоставления одного project link с одной SKDF road.
- `source` показывает, как получено совпадение:
  - `direct`
  - `osm_way_group`
  - `osm_way_propagated`

### `load_skdf_roads`

```python
with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
    sample = f.read(4096)
    f.seek(0)
    delimiter = _detect_csv_delimiter(sample)
    reader = csv.DictReader(f, delimiter=delimiter)
```

Что происходит:

- CSV читается с учетом BOM через `utf-8-sig`.
- Разделитель определяется автоматически: `;` или `,`.

```python
geometry = _parse_skdf_geometry(row.get("geometry"), MultiLineString)
if geometry is None or geometry.is_empty:
    continue
```

Что происходит:

- Строки без валидной геометрии отбрасываются.

```python
roads.append(
    SkdfRoad(
        row_index=row_index,
        road_id=_text(row.get("road_id")),
        ...
        traffic=_number(row.get("traffic_1")),
        capacity=_number(row.get("capacity_1")),
        lanes=_int_number(row.get("lanes_1")),
        speed_limit=_number(row.get("speed_limit_1")),
        geometry=geometry,
        normalized_name=normalize_road_name(name),
    )
)
```

Что происходит:

- Из CSV забираются значения трафика, capacity, полос и скорости.
- Числа проходят через безопасный парсер `_number`.

### `enrich_project_with_skdf`

```python
config = config or SkdfMatchConfig()
roads = load_skdf_roads(csv_path)
matcher = _SkdfMatcher(roads, config)
```

Что происходит:

- Загружаются дороги СКДФ.
- Создается matcher с пространственным индексом.

```python
for link in project.network.links.values():
    line = _link_geometry_3857(link)
    link_geometries[link.id] = line
    if line is None or line.is_empty:
        continue

    links_with_geometry += 1
    match = matcher.match_link(link, line)
    best_candidates[link.id] = matcher.best_candidate(link.name, line, respect_acceptance=False)
```

Что происходит:

- Геометрия link переводится в EPSG:3857.
- Для каждого link ищется принятое совпадение.
- Отдельно сохраняется лучший кандидат без acceptance-фильтров, чтобы отчет показывал, что было рядом даже при отказе.

```python
if config.way_group_enabled:
    _assign_way_group_matches(project, matcher, link_geometries, matches, best_candidates)
```

Что происходит:

- Если часть links одного OSM way не сопоставилась напрямую, включается групповая логика.
- Это важно для случая, когда OSM way был нарезан на несколько project links.

```python
updated_traffic, updated_capacity = _apply_match(link, match)
links_updated_traffic += int(updated_traffic)
links_updated_capacity += int(updated_capacity)
report_rows.append(_report_row(link, match, best_candidate, f"matched_{match.source}"))
```

Что происходит:

- Принятый match применяется к link.
- Формируется строка CSV-отчета.

```python
project.metadata = {
    **(project.metadata or {}),
    "skdf_enrichment": {
        "csv_path": str(csv_path),
        "skdf_roads_loaded": stats.skdf_roads_loaded,
        ...
    },
}
```

Что происходит:

- В metadata проекта записывается статистика обогащения.

### `_SkdfMatcher`

```python
self.geometries = [road.geometry for road in roads]
self.index = STRtree(self.geometries)
self.geometry_to_road = {id(road.geometry): road for road in roads}
```

Что происходит:

- Создается `STRtree` - пространственный индекс shapely.
- Он ускоряет поиск близких SKDF-геометрий.

### `best_candidate`

```python
search_area = line.buffer(self.config.max_distance_m)
candidates = self.index.query(search_area)
```

Что происходит:

- Вокруг link строится буфер.
- Из индекса запрашиваются SKDF-линии, которые попадают в эту область.

```python
distance = line.distance(road.geometry)
if distance > self.config.max_distance_m:
    continue
```

Что происходит:

- Кандидаты дальше максимальной дистанции отбрасываются.

```python
overlap_ratio = _safe_ratio(
    line.intersection(road.geometry.buffer(self.config.buffer_m)).length,
    line.length,
)
if respect_acceptance and overlap_ratio < self.config.min_overlap_ratio:
    continue
```

Что происходит:

- SKDF road расширяется буфером.
- Считается, какая доля OSM-link лежит внутри этого буфера.
- Если overlap слишком мал, match не принимается.

```python
name_similarity = _name_similarity(name, road.normalized_name)
if respect_acceptance and (
    self.config.reject_named_mismatches
    and name_similarity < 0
    and not _name_mismatch_override_allowed(distance, overlap_ratio, self.config)
):
    continue
```

Что происходит:

- Названия сравниваются после нормализации.
- При явном конфликте названий match отбрасывается, если не разрешен strong geometry override.

### `_apply_match`

```python
if road.traffic is not None:
    link.traffic_counts = {**(link.traffic_counts or {}), "car": road.traffic}
    updated_traffic = True
```

Что происходит:

- Трафик СКДФ записывается в `link.traffic_counts["car"]`.

```python
if road.lanes is not None:
    link.parameters["lanes_total"] = max(road.lanes, 1)

lanes = _int_number(link.parameters.get("lanes_total")) or 1
if road.capacity is not None:
    link.parameters["capacity_per_lane_base"] = round(road.capacity / max(lanes, 1), 3)
    link.parameters["capacity_total_skdf"] = road.capacity
```

Что происходит:

- Полосы записываются в параметры link.
- Общая capacity СКДФ делится на количество полос, чтобы получить `capacity_per_lane_base`.
- Исходная общая capacity сохраняется как `capacity_total_skdf`.

```python
link.metadata = {
    **(link.metadata or {}),
    "skdf": {
        "road_id": road.road_id,
        ...
        "match_score": round(match.score, 4),
        "match_distance_m": round(match.distance_m, 2),
        "match_overlap_ratio": round(match.overlap_ratio, 4),
        "match_source": match.source,
    },
}
link.results = {}
```

Что происходит:

- В metadata сохраняется объяснимость match.
- `link.results` очищается, потому что после изменения traffic/capacity старый анализ недействителен.

### `_assign_way_group_matches`

```python
links_by_way_id: dict[str, list[Link]] = defaultdict(list)
for link in project.network.links.values():
    way_id = str((link.metadata or {}).get("osm_way_id") or "").strip()
    if way_id:
        links_by_way_id[way_id].append(link)
```

Что происходит:

- Links группируются по исходному `osm_way_id`.

```python
group_geometry = unary_union(geometries)
group_match = matcher.match_geometry(
    link_id=links[0].id,
    name=group_name,
    line=group_geometry,
    source="osm_way_group",
)
```

Что происходит:

- Для группы links строится объединенная геометрия.
- Она сопоставляется с SKDF как единый объект.

```python
dominant_road_id, dominant_count = Counter(matches[link.id].road.road_id for link in matched_links).most_common(1)[0]
...
source="osm_way_propagated"
```

Что происходит:

- Если большинство links в группе уже сопоставлены с одной SKDF road, это сопоставление распространяется на оставшиеся links.

### `_link_geometry_3857`

```python
if coords.get("type") == "polyline":
    points = coords.get("points", [])
else:
    points = [
        (coords.get("lon_start"), coords.get("lat_start")),
        (coords.get("lon_end"), coords.get("lat_end")),
    ]
```

Что происходит:

- Link может хранить либо полилинию, либо только начало/конец.
- В обоих случаях строится список lon/lat точек.

```python
transformer = _get_4326_to_3857_transformer()
projected_points = [transformer.transform(lon, lat) for lon, lat in clean_points]
return LineString(projected_points)
```

Что происходит:

- Геометрия переводится из EPSG:4326 в EPSG:3857.
- Это важно, потому что расстояния и буферы считаются в метрах.

### `_match_score`

```python
distance_score = max(0.0, 1.0 - distance_m / config.max_distance_m)
score = 0.65 * overlap_ratio + 0.35 * distance_score
if name_similarity > 0:
    score += config.name_bonus * name_similarity
elif name_similarity < 0:
    score -= config.name_mismatch_penalty
return score
```

Что происходит:

- Итоговый score состоит из overlap и distance.
- Совпадение названий добавляет бонус.
- Конфликт названий дает штраф.

### `normalize_road_name`

```python
text = _text(value).lower().replace("ё", "е")
text = re.sub(r"[.,;:()\"'`]", " ", text)
...
tokens = [replacements.get(token, token) for token in re.split(r"\s+", text) if token]
return " ".join(tokens)
```

Что происходит:

- Названия приводятся к нижнему регистру.
- Удаляется пунктуация.
- Типы улиц нормализуются: улица, проспект, переулок и т.п.

### `_report_row` и `_write_report`

```python
row = {
    "link_id": link.id,
    "link_name": link.name,
    "status": status,
    ...
    "best_candidate_road_id": "",
}
```

Что происходит:

- Для каждого link формируется строка отчета.
- Даже если match не принят, в отчет может попасть лучший кандидат.

```python
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
```

Что происходит:

- CSV сохраняется с BOM, чтобы его проще открывать в Excel.

## 5. `enrich_project_with_skdf.py`

Файл - командная обертка вокруг `skdf_matcher.py`.

### CLI-аргументы

```python
parser.add_argument("--project", default="osm_network_project_map_bez.json")
parser.add_argument("--skdf-csv", default="nsk_roads_bbox.csv")
parser.add_argument("--output", default="osm_network_project_skdf_map_bez.json")
parser.add_argument("--report", default="skdf_match_report.csv")
```

Что происходит:

- Задаются входной проект, CSV СКДФ, выходной проект и отчет сопоставления.
- Значения по умолчанию сейчас ориентированы на `map_bez`.

```python
parser.add_argument("--max-distance-m", type=float, default=35.0)
parser.add_argument("--buffer-m", type=float, default=25.0)
parser.add_argument("--min-overlap-ratio", type=float, default=0.45)
parser.add_argument("--min-score", type=float, default=0.55)
```

Что происходит:

- Эти параметры напрямую попадают в `SkdfMatchConfig`.
- Через CLI можно менять жесткость сопоставления без редактирования кода.

### Основной запуск

```python
project = ProjectLoader().load(args.project)
config = SkdfMatchConfig(
    max_distance_m=args.max_distance_m,
    buffer_m=args.buffer_m,
    min_overlap_ratio=args.min_overlap_ratio,
    min_score=args.min_score,
    reject_named_mismatches=not args.allow_name_mismatches,
    allow_strong_geometry_name_override=args.allow_name_mismatch_overrides,
)
```

Что происходит:

- Проект загружается из JSON.
- Создается конфигурация matcher-а.

```python
stats = enrich_project_with_skdf(
    project,
    args.skdf_csv,
    config=config,
    report_path=args.report,
)
ProjectSaver().save(project, args.output)
```

Что происходит:

- `skdf_matcher.enrich_project_with_skdf` изменяет объект `project` на месте.
- После этого проект сохраняется в новый JSON.

Связь:

- Этот файл не содержит алгоритма сопоставления. Он только связывает загрузку, matcher и сохранение.

## 6. `demand_model_utils.py`

Файл содержит общие функции валидации и нормализации demand-модели.

### Константы

```python
VALID_DEMAND_TYPES = {"routes", "route_split_coefficients"}
VALID_DEMAND_UNITS = {"veh/h", "pcu/h"}
FORBIDDEN_SPLIT_DEMAND_KEYS = {"demand_value", "demand_veh_h", "demand"}
```

Что происходит:

- Поддерживаются два типа demand-модели:
  - `routes`: маршруты уже содержат абсолютный спрос;
  - `route_split_coefficients`: спрос считается как boundary flow * coefficient.
- Единицы: автомобили в час или приведенные автомобили в час.
- В split-модели нельзя задавать `demand_value` напрямую, потому что он вычисляется.

### `validate_route_path`

```python
if not link_ids:
    if require_links:
        errors.append(f"{label}: link_ids are required.")
    return errors
```

Что происходит:

- Маршрут должен содержать links, если `require_links=True`.

```python
for link_id in link_ids:
    if link_id not in network.links:
        errors.append(f"{label}: missing link {link_id}.")
if errors:
    return errors
```

Что происходит:

- Проверяется существование каждого link.
- Если есть отсутствующие links, дальнейшая проверка связности не выполняется.

```python
disabled_links = [
    link_id for link_id in link_ids if network.links[link_id].metadata.get("disabled")
]
if disabled_links:
    errors.append(...)
```

Что происходит:

- Demand-маршрут не должен использовать отключенные links.

```python
for previous_link_id, next_link_id in zip(link_ids, link_ids[1:]):
    previous_link = network.links[previous_link_id]
    next_link = network.links[next_link_id]
    if previous_link.end_node_id != next_link.start_node_id:
        errors.append(...)
```

Что происходит:

- Проверяется топологическая связность маршрута.
- Конец предыдущего link должен совпадать с началом следующего.

### `as_float` и `read_required_nonnegative`

```python
def as_float(value: Any, label: str, errors: list[str]) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"{label}: must be a number.")
        return None
```

Что происходит:

- Безопасное преобразование в число.
- Ошибка не выбрасывается, а добавляется в список.

```python
if value is None:
    errors.append(f"{label}: is required.")
    return None
...
if number < 0:
    errors.append(f"{label}: cannot be negative.")
    return None
```

Что происходит:

- Проверяется обязательность и неотрицательность значения.

### `build_route_report`

```python
report = {
    "id": route_id,
    "name": source.get("name", route_id),
    "origin_node_id": origin,
    "destination_node_id": destination,
    "demand_value": demand,
    "unit": unit,
    "vehicle_type": "pcu" if unit == "pcu/h" else source.get("vehicle_type", "car"),
    "link_ids": list(link_ids),
}
```

Что происходит:

- Формируется единый формат маршрута для отчета и назначения потоков.
- Если unit `pcu/h`, vehicle_type становится `pcu`.
- Иначе берется vehicle_type из маршрута или `car`.

## 7. `demand_assignment_service.py`

Файл применяет `project.demand_model` к сети: считает потоки по links и записывает их в `link.traffic_counts`.

### Константы класса

```python
class DemandAssignmentService:
    MODE_ROUTES = "routes"
    MODE_ROUTE_SPLITS = "route_split_coefficients"
    BALANCE_POLICY_ALLOW_UNASSIGNED = "allow_unassigned"
    COEFFICIENT_TOLERANCE = 1e-5
```

Что происходит:

- Задает два режима demand-модели.
- `allow_unassigned` разрешает, чтобы сумма коэффициентов была меньше 1.
- `COEFFICIENT_TOLERANCE` нужен для сравнения float-коэффициентов.

### `assign`

```python
demand_model = project.demand_model or {}
model_type = demand_model.get("type")
unit = demand_model.get("unit", "veh/h")

if not demand_model:
    return self._report(None, unit, [], {}, warnings, errors)
```

Что происходит:

- Если demand-модели нет, сервис возвращает пустой успешный/нейтральный отчет.

```python
if model_type not in VALID_DEMAND_TYPES:
    errors.append("demand_model.type must be 'routes' or 'route_split_coefficients'.")
if unit not in VALID_DEMAND_UNITS:
    errors.append("demand_model.unit must be 'veh/h' or 'pcu/h'.")
```

Что происходит:

- Проверяется тип модели и единицы измерения.

```python
if not errors and model_type == self.MODE_ROUTES:
    prepared_routes = self._prepare_routes(project, unit, errors)
    self._warn_routes_boundary_balance(project, prepared_routes, warnings)
elif not errors and model_type == self.MODE_ROUTE_SPLITS:
    prepared_routes, flow_summary = self._prepare_route_splits(project, unit, warnings, errors)
```

Что происходит:

- Для режима `routes` берутся готовые абсолютные demand values.
- Для режима `route_split_coefficients` спрос вычисляется из boundary flows и коэффициентов.

```python
link_assignments = self._compute_link_assignments(prepared_routes)
self._apply_assignments(project, link_assignments)
```

Что происходит:

- Спрос агрегируется по links.
- Затем link.traffic_counts перезаписываются назначенными потоками.

### `_prepare_routes`

```python
routes = project.demand_model.get("routes", [])
if not isinstance(routes, list) or not routes:
    errors.append("demand_model.routes must contain at least one route.")
    return prepared
```

Что происходит:

- В режиме `routes` должен быть непустой список маршрутов.

```python
demand = read_required_nonnegative(
    route.get("demand_value"), f"{label} demand_value", errors
)
link_ids = list(route.get("link_ids") or [])
origin = route.get("origin_node_id") or route.get("from")
destination = route.get("destination_node_id") or route.get("to")
```

Что происходит:

- Читаются demand, links, origin и destination.
- Поддерживаются алиасы `from` и `to`.

```python
errors.extend(
    validate_route_path(
        project.network,
        label,
        link_ids,
        origin,
        destination,
        require_links=True,
    )
)
```

Что происходит:

- Маршрут проверяется на существование links, связность и совпадение origin/destination.

### `_prepare_route_splits`

```python
boundary_flows = demand_model.get("boundary_flows", {})
route_splits = demand_model.get(self.MODE_ROUTE_SPLITS, [])
balance_policy = demand_model.get("split_balance_policy")
```

Что происходит:

- `boundary_flows` задает входной поток на boundary-узлах.
- `route_split_coefficients` задает доли распределения этого потока по маршрутам.

```python
for boundary_id, raw_volume in boundary_flows.items():
    volume = as_float(raw_volume, label, errors)
    ...
    if boundary_id not in project.network.nodes:
        errors.append(f"{label}: node is missing.")
```

Что происходит:

- Boundary flow должен быть числом.
- Boundary node должен существовать в сети.

```python
forbidden_keys = sorted(FORBIDDEN_SPLIT_DEMAND_KEYS.intersection(split))
if forbidden_keys:
    errors.append(
        f"{label}: demand is calculated from boundary_flows and coefficient; "
        f"remove {', '.join(forbidden_keys)}."
    )
```

Что происходит:

- В split-модели запрещено вручную задавать demand.
- Demand считается только как `boundary_flow * coefficient`.

```python
coefficient = read_required_nonnegative(
    split.get("coefficient"), f"{label} coefficient", errors
)
if coefficient is not None and coefficient > 1:
    errors.append(f"{label}: coefficient cannot be greater than 1.")
```

Что происходит:

- Коэффициент обязателен.
- Он должен быть в диапазоне от 0 до 1.

```python
demand = boundary_values.get(origin, 0.0) * coefficient
prepared.append(
    build_route_report(
        split,
        split_id,
        origin,
        destination,
        demand,
        unit,
        link_ids,
        coefficient=coefficient,
        boundary_flow=boundary_values.get(origin),
    )
)
```

Что происходит:

- Абсолютный спрос маршрута вычисляется из входного потока и коэффициента.
- В отчет добавляются coefficient и boundary_flow.

### `_validate_coefficient_sums`

```python
if coefficient_sum > 1.0 + tolerance:
    errors.append(...)
elif coefficient_sum < 1.0 - tolerance:
    if policy == self.BALANCE_POLICY_ALLOW_UNASSIGNED:
        warnings.append(message)
    else:
        errors.append(message)
```

Что происходит:

- Сумма коэффициентов от одного origin не должна быть больше 1.
- Если меньше 1, это ошибка, кроме политики `allow_unassigned`.

### `_compute_link_assignments`

```python
for route in routes:
    demand = float(route["demand_value"])
    if demand <= 0:
        continue
    vehicle_key = route["vehicle_type"]
    for link_id in route["link_ids"]:
        link_counts = assignments.setdefault(link_id, {})
        link_counts[vehicle_key] = link_counts.get(vehicle_key, 0.0) + demand
```

Что происходит:

- Каждый маршрут добавляет свой demand на каждый link маршрута.
- Если несколько маршрутов проходят через один link, потоки суммируются.

### `_apply_assignments`

```python
if (
    link.traffic_counts
    and link.metadata.get("traffic_counts_source") != "assigned_demand"
    and "observed_traffic_counts" not in link.metadata
):
    link.metadata["observed_traffic_counts"] = dict(link.traffic_counts)
```

Что происходит:

- Перед перезаписью traffic_counts исходные наблюдаемые потоки сохраняются в metadata.
- Это важно, чтобы demand assignment не уничтожил исходные данные.

```python
link.traffic_counts = assigned_counts
if assigned_counts:
    link.metadata["traffic_counts_source"] = "assigned_demand"
elif link.metadata.get("traffic_counts_source") == "assigned_demand":
    link.metadata.pop("traffic_counts_source", None)
link.results = {}
```

Что происходит:

- Link получает назначенные потоки.
- Ставится источник `assigned_demand`.
- Результаты анализа очищаются, потому что traffic изменился.

### `_report`

```python
return {
    "success": not errors,
    "demand_model_type": model_type,
    "unit": unit,
    "assigned_routes": 0 if errors else len(active_routes),
    "routes": [] if errors else routes,
    "link_assignments": {} if errors else link_assignments,
    "boundary_flow_summary": flow_summary or {},
    "warnings": warnings,
    "errors": errors,
}
```

Что происходит:

- Возвращается структурированный отчет.
- Если есть ошибки, routes и assignments очищаются, чтобы не использовать частично некорректные данные.

## 8. `demand_model_wizard.py`

Файл содержит автоматический генератор черновой demand-модели и PyQt-диалог для его запуска.

### Константы

```python
IMPORTANT_HIGHWAYS = {"primary", "secondary", "tertiary"}
DEFAULT_BOUNDARY_MARGIN_PERCENT = 7
DEFAULT_MAX_DESTINATIONS_PER_ORIGIN = 3
BOUNDARY_CLUSTER_RADIUS_KM = 0.12
```

Что происходит:

- Boundary-узлы по умолчанию ищутся на важных дорогах.
- Узел считается около края карты, если он попадает в граничную область шириной 7% от bbox.
- Близкие boundary-кандидаты кластеризуются в радиусе 120 метров.

### `AutoDemandBuildResult`

```python
@dataclass
class AutoDemandBuildResult:
    boundary_nodes: list[str]
    demand_origins: list[str]
    intersections: list[str]
    roundabout_parts: list[str]
    routes: list[dict[str, Any]]
    warnings: list[str]
```

Что происходит:

- Это результат генерации demand-модели.
- Используется для preview в GUI.

### `AutoDemandBuilder.apply`

```python
network = project.network
warnings: list[str] = []

self._classify_nodes(network)
boundary_nodes = self._detect_boundary_nodes(network)
```

Что происходит:

- Сначала классифицируются узлы.
- Потом определяются boundary-узлы.

```python
routes = self._build_route_splits(network, boundary_nodes, warnings)
demand_origins = sorted({route["from"] for route in routes})
exit_only_nodes = sorted(set(boundary_nodes) - set(demand_origins))
```

Что происходит:

- Строятся маршруты между boundary-узлами.
- Узлы, из которых не получилось построить исходящие маршруты, остаются только выходами.

```python
project.demand_model = {
    "type": "route_split_coefficients",
    "unit": "veh/h",
    "description": "...",
    "boundary_flows": {node_id: self.default_boundary_flow for node_id in demand_origins},
    "route_split_coefficients": routes,
    "metadata": {...},
}
```

Что происходит:

- В проект записывается черновая demand-модель.
- Она использует split-коэффициенты, а не абсолютные route demand.
- Boundary flows одинаковые и служат заглушками.

```python
project.metadata.setdefault("demand_model_notes", []).append(
    "Auto-generated demand model is a draft. Boundary flows and split coefficients are placeholders."
)
```

Что происходит:

- В metadata явно фиксируется, что demand-модель черновая.

### `_classify_nodes`

```python
incoming = self._incoming(network)
outgoing = self._outgoing(network)
for node_id, node in network.nodes.items():
    if node.metadata.get("manual_node_type"):
        node.node_type = str(node.metadata["manual_node_type"])
```

Что происходит:

- Если пользователь вручную задал тип узла, он имеет приоритет.

```python
in_degree = len(incoming.get(node_id, []))
out_degree = len(outgoing.get(node_id, []))
degree = len({link.id for link in incoming.get(node_id, []) + outgoing.get(node_id, [])})
node.metadata["in_degree"] = in_degree
node.metadata["out_degree"] = out_degree
node.metadata["degree"] = degree
```

Что происходит:

- В metadata узла записываются степени графа.
- Это потом отображается в `traffic_viz.py`.

```python
if self._looks_like_roundabout_part(network, node_id, incoming, outgoing):
    node.node_type = "roundabout_part"
elif degree >= 3:
    node.node_type = "intersection"
elif node.node_type not in {"boundary", "roundabout_part"}:
    node.node_type = "ordinary"
```

Что происходит:

- Узлы классифицируются как часть кольца, перекресток или обычный узел.

### `_detect_boundary_nodes`

```python
nodes_with_coords = [node for node in network.nodes.values() if node.lon is not None and node.lat is not None]
...
lon_margin = max((lon_max - lon_min) * self.boundary_margin_percent / 100.0, 1e-9)
lat_margin = max((lat_max - lat_min) * self.boundary_margin_percent / 100.0, 1e-9)
```

Что происходит:

- Считается bbox сети.
- Граничная зона определяется в процентах от размера bbox.

```python
if not self.include_residential_boundaries and not self._has_important_incident_highway(incident):
    continue
near_border = (
    float(node.lon) <= lon_min + lon_margin
    or float(node.lon) >= lon_max - lon_margin
    or float(node.lat) <= lat_min + lat_margin
    or float(node.lat) >= lat_max - lat_margin
)
if near_border:
    candidates.append(node.id)
```

Что происходит:

- Узел должен быть около края карты.
- По умолчанию residential-узлы не считаются boundary, если нет важной дороги.

```python
boundary_nodes = self._cluster_boundary_candidates(...)
for node_id in boundary_nodes:
    network.nodes[node_id].node_type = "boundary"
    network.nodes[node_id].metadata["boundary_candidate"] = True
```

Что происходит:

- Близкие candidates объединяются.
- Представители кластеров становятся boundary-узлами.

### `_build_route_splits`

```python
for origin in boundary_nodes:
    destinations = []
    for destination in boundary_nodes:
        if destination == origin:
            continue
        path, length = self._shortest_path(network, origin, destination)
        if path:
            destinations.append((length, destination, path))
```

Что происходит:

- Для каждого boundary-origin ищутся маршруты до других boundary-узлов.
- Используется направленный кратчайший путь.

```python
destinations.sort(key=lambda item: item[0])
destinations = destinations[: self.max_destinations_per_origin]
coefficient = 1.0 / len(destinations)
```

Что происходит:

- Берутся ближайшие destination-узлы.
- Коэффициенты распределяются равномерно.

```python
route_splits.append(
    {
        "id": f"RS_{self._safe_id(origin)}_{self._safe_id(destination)}",
        "from": origin,
        "to": destination,
        "coefficient": round(coefficient, 8),
        "vehicle_type": "car",
        "link_ids": path,
    }
)
```

Что происходит:

- Создается запись demand route split.
- Абсолютный demand здесь не задается. Его потом рассчитает `DemandAssignmentService`.

### `_shortest_path`

```python
queue: list[tuple[float, str, list[str]]] = [(0.0, origin, [])]
best: dict[str, float] = {origin: 0.0}
...
for link in outgoing.get(node_id, []):
    if link.metadata.get("disabled"):
        continue
    next_cost = cost + max(float(link.length_km or 0.001), 0.001)
```

Что происходит:

- Это локальная реализация Дейкстры.
- Вес маршрута - длина links.
- Отключенные links не используются.

### `DemandModelWizard`

```python
class DemandModelWizard(QDialog):
    def __init__(self, project_file: str, parent=None):
        self.project_file = project_file
        self.project: Project | None = None
        self.last_result: AutoDemandBuildResult | None = None
```

Что происходит:

- Диалог работает с конкретным JSON-файлом проекта.
- Загружает проект, генерирует demand_model, сохраняет обратно.

```python
self.flow_spin = QDoubleSpinBox()
self.flow_spin.setRange(0, 100000)
self.flow_spin.setValue(1200)
...
self.margin_spin.setRange(1, 30)
self.max_dest_spin.setRange(1, 10)
```

Что происходит:

- Пользователь задает:
  - граничный поток;
  - процент края карты;
  - максимум выходов на один вход.

```python
builder = AutoDemandBuilder(
    default_boundary_flow=self.flow_spin.value(),
    boundary_margin_percent=self.margin_spin.value(),
    max_destinations_per_origin=self.max_dest_spin.value(),
    include_residential_boundaries=self.include_residential.isChecked(),
)
self.last_result = builder.apply(self.project)
```

Что происходит:

- По настройкам GUI создается builder.
- Builder изменяет `project.demand_model`.

```python
ProjectSaver().save(self.project, self.project_file)
```

Что происходит:

- Demand-модель сохраняется в тот же JSON проекта.

### `_format_result`

```python
lines = [
    "Auto demand_model draft",
    f"boundary nodes: {len(result.boundary_nodes)}",
    f"demand origins: {len(result.demand_origins)}",
    ...
]
```

Что происходит:

- Формируется текст preview для пользователя.
- Показываются boundary nodes, generated routes и warnings.

## 9. `traffic_viz.py`

Файл визуализирует проект, расчетные результаты, маршруты и demand-состояние.

### Импорты и сервисы

```python
from analysis_service import AnalysisService
from project_loader import ProjectLoader
from project_saver import ProjectSaver
from routing_service import RoutingService
```

Что происходит:

- Визуализатор не только рисует граф.
- Он загружает проект, запускает анализ, сохраняет координаты и ищет маршруты.

### Координатные функции

```python
def project_coords(lon, lat):
    if USE_PYPROJ:
        x, y = transformer.transform(lon, lat)
        return x, -y
    ...
    return x, -y
```

Что происходит:

- lon/lat переводятся в координаты сцены.
- Y инвертируется для Qt.

```python
def unproject_coords(x, y_qt):
    if USE_PYPROJ:
        lon, lat = inv_transformer.transform(x, -y_qt)
        return lon, lat
```

Что происходит:

- Координаты сцены переводятся обратно в lon/lat перед сохранением.

### Цвета

```python
LOS_COLORS = {
    "A": QColor(0, 200, 0),
    ...
    "F": QColor(255, 0, 0),
    "UNDEFINED": QColor(200, 200, 200),
}

NODE_COLORS = {
    "boundary": QColor(220, 40, 40),
    "intersection": QColor(45, 90, 210),
    "roundabout_part": QColor(150, 70, 210),
    "ordinary": QColor(80, 80, 80),
}
```

Что происходит:

- Links раскрашиваются по LOS.
- Nodes раскрашиваются по `node_type`, который может быть выставлен demand wizard-ом.

### `MapBackgroundItem`

```python
self.roads = map_data.get("roads", [])
self.buildings = map_data.get("buildings", [])
...
self.setZValue(-100)
```

Что происходит:

- Фоновая карта рисуется на заднем плане.
- Содержит дороги и здания из OSM-файла.

### `TrafficNode`

```python
class TrafficNode(QGraphicsEllipseItem):
    def __init__(self, node_model, label, pos_point, app_callback=None):
        self.node_model = node_model
        self.node_id = node_model.id
        self.app_callback = app_callback
        self.setBrush(QBrush(NODE_COLORS.get(node_model.node_type, NODE_COLORS["ordinary"])))
```

Что происходит:

- Графический узел хранит весь `node_model`.
- Цвет зависит от типа узла.
- При клике вызывается callback.

```python
def itemChange(self, change, value):
    if change == QGraphicsItem.ItemPositionChange:
        for link in self.connected_links:
            link.update_geometry()
```

Что происходит:

- При перемещении узла перестраиваются связанные links.

### `TrafficLink`

```python
coords = link_model.coords or {}
if coords.get("type") == "polyline":
    raw_points = coords.get("points", [])
    if len(raw_points) > 2:
        for p in raw_points[1:-1]:
            self.intermediate_points.append(project_coords(p[0], p[1]))
```

Что происходит:

- Link может быть полилинией, а не прямой.
- Визуализатор сохраняет промежуточные точки, чтобы рисовать реальную форму дороги.

```python
def update_geometry(self):
    self.prepareGeometryChange()
    path = QPainterPath()
    path.moveTo(self.start_node.scenePos())
    for pt in self.intermediate_points:
        path.lineTo(QPointF(pt[0], pt[1]))
    path.lineTo(self.end_node.scenePos())
    self.setPath(path)
```

Что происходит:

- Геометрия link обновляется при движении узлов.
- `prepareGeometryChange()` нужен Qt, чтобы корректно обновить bounding rect.

```python
def update_visuals(self, stage):
    res = self.link_model.results or {}
    ...
    if stage == 1:
        color = LOS_COLORS.get(res.get("LOS", "UNDEFINED"), Qt.gray)
    elif stage == 2:
        if res.get("Optimization_Proposal"):
            color = QColor(255, 0, 0)
            width = 12
    elif stage == 3:
        delay = res.get("Delay_sec", 0)
    elif stage == 4:
        if self.is_route_highlighted:
            color = QColor(0, 170, 255)
            width = 12
```

Что происходит:

- Режим 1: LOS.
- Режим 2: оптимизация.
- Режим 3: задержки.
- Режим 4: найденный маршрут.

```python
def paint(self, painter, option, widget):
    super().paint(painter, option, widget)
    path = self.path()
    if path.length() < 15:
        return
    percent = 0.9
    point = path.pointAtPercent(percent)
    angle = path.angleAtPercent(percent)
    ...
    painter.drawPolygon(arrow_head)
```

Что происходит:

- После линии рисуется стрелка направления.
- Стрелка ставится ближе к концу link.

### `MainWindow.__init__`

```python
self.loader = ProjectLoader()
self.saver = ProjectSaver()
self.analysis_service = AnalysisService()
self.routing_service = RoutingService()
```

Что происходит:

- Главное окно держит сервисы загрузки, сохранения, анализа и маршрутизации.

```python
self.btn_demand_wizard = QPushButton("Автогенерация demand_model")
self.btn_demand_wizard.clicked.connect(self.open_demand_model_wizard)
```

Что происходит:

- В интерфейс добавлена кнопка demand wizard-а.
- Через нее пользователь генерирует `project.demand_model`.

### `open_demand_model_wizard`

```python
from demand_model_wizard import DemandModelWizard
dialog = DemandModelWizard(self.data_file, self)
if dialog.exec_():
    self.reload_project_and_redraw()
```

Что происходит:

- Demand wizard импортируется динамически.
- После успешного сохранения проект перезагружается и перерисовывается.

### `reload_project_and_redraw`

```python
self.scene.clear()
self.viz_links = []
self.link_index = {}
self.node_index = {}
self.demand_report_text = ""
self.draw_map()
self.load_project_data(self.data_file)
self.draw_network()
self.set_stage(1)
```

Что происходит:

- Полная перезагрузка визуального состояния.
- Используется после demand wizard-а и при стартовом запуске.

### `load_project_data`

```python
self.project = self.loader.load(path)
needs_analysis = self._has_demand_model() or any(not link.results for link in self.project.network.links.values())
if needs_analysis:
    report = self.analysis_service.analyze_project(self.project)
    self._show_demand_report(report)
```

Что происходит:

- Если есть demand-модель, анализ запускается всегда.
- Это логично: demand assignment может менять `traffic_counts`, значит results надо пересчитать.

### `_show_demand_report`

```python
status = report.get("Analysis_Status")
assignment_report = report.get("Demand_Assignment", {})
text = self._format_demand_summary(status, assignment_report, report)
self.demand_report_text = text
self.info.setPlainText(text)
```

Что происходит:

- Из отчета `AnalysisService` извлекается demand assignment.
- Summary показывается в правой панели.

```python
if status in {"Validation failed", "Demand assignment failed"}:
    QMessageBox.critical(self, "Demand assignment", text)
    return
```

Что происходит:

- При ошибках пользователь получает критическое окно.

### `_demand_summary_lines`

```python
lines = [
    "Demand assignment",
    f"demand_model.type: {demand_model.get('type')}",
    f"status: {status}",
    f"assigned_routes: {assignment_report.get('assigned_routes', 0)}",
]
```

Что происходит:

- Формируется понятный текстовый summary по demand assignment.

```python
for origin, summary in assignment_report.get("boundary_flow_summary", {}).items():
    lines.append(
        f"{origin}: boundary={summary.get('boundary_flow')}, "
        f"assigned={summary.get('assigned_flow')}, unassigned={summary.get('unassigned_flow')}"
    )
```

Что происходит:

- Для каждого boundary-origin показывается входной, назначенный и неназначенный поток.

### `draw_network`

```python
for node_model in self.project.network.nodes.values():
    if node_model.lon is not None and node_model.lat is not None:
        point = QPointF(*project_coords(node_model.lon, node_model.lat))
    elif node_model.x is not None and node_model.y is not None:
        point = QPointF(node_model.x, node_model.y)
    ...
    node_item = TrafficNode(node_model, node_label, point, self.on_node_click)
```

Что происходит:

- Узлы рисуются из lon/lat или из сохраненных сценовых координат.
- Клик по узлу показывает подробности.

```python
for link_model in self.project.network.links.values():
    start_node = node_registry.get(link_model.start_node_id)
    end_node = node_registry.get(link_model.end_node_id)
    if start_node is None or end_node is None:
        continue
    link_item = TrafficLink(link_model, start_node, end_node, self.on_link_click)
```

Что происходит:

- Links рисуются только если оба узла существуют на сцене.

### `on_node_click`

```python
incoming = self.project.network.get_incoming_links(node_model.id)
outgoing = self.project.network.get_outgoing_links(node_model.id)
...
html += f"<b>Тип:</b> {node_model.node_type}<br>"
html += f"<b>Входящих link:</b> {len(incoming)}<br>"
html += f"<b>Исходящих link:</b> {len(outgoing)}<br>"
```

Что происходит:

- В правой панели показывается информация об узле.
- Это важно для demand-модели: видно boundary/intersection/roundabout_part.

### `on_link_click`

```python
html += f"<b>from → to:</b> {link_model.start_node_id} → {link_model.end_node_id}<br>"
html += f"<b>Поток:</b> {link_model.traffic_counts}<br><br>"
```

Что происходит:

- Показывается направление link и текущие traffic_counts.
- Если demand assignment уже прошел, здесь будут назначенные demand-потоки.

### `find_route`

```python
path_link_ids = self.routing_service.find_shortest_path(
    self.project.network,
    display_to_id[start_display],
    display_to_id[end_display],
    weight,
)
```

Что происходит:

- Ручной поиск маршрута использует `RoutingService`, не demand wizard.
- Это отдельный инструмент просмотра сети.

### `save_current_positions_to_project`

```python
for link in self.viz_links:
    p1 = link.start_node.scenePos()
    p2 = link.end_node.scenePos()
    lon_s, lat_s = unproject_coords(p1.x(), p1.y())
    lon_e, lat_e = unproject_coords(p2.x(), p2.y())
```

Что происходит:

- Текущие позиции start/end узлов links переводятся обратно в lon/lat.
- Затем сохраняются в `link.coords`.

## 10. Важные связи с `models.py`

Актуальная модель проекта содержит demand-поля:

```python
@dataclass
class Route:
    id: str
    name: str
    link_ids: list[str] = field(default_factory=list)
    origin_node_id: str | None = None
    destination_node_id: str | None = None
    demand_value: float | None = None
    vehicle_type: str = "car"
    results: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
```

Что важно:

- `Route` теперь может быть не просто визуальным маршрутом, а маршрутом спроса.
- Однако demand split routes чаще лежат не в `network.routes`, а в `project.demand_model`.

```python
@dataclass
class Project:
    project_name: str = "Unnamed Project"
    pcu_coefficients: dict[str, float] = field(default_factory=dict)
    network: Network = field(default_factory=Network)
    scenarios: list[Scenario] = field(default_factory=list)
    demand_model: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
```

Что важно:

- `Project.demand_model` - точка входа для всего demand-пайплайна.
- Если поле непустое, `AnalysisService` запускает `DemandAssignmentService`.

## 11. Важные связи с `project_loader.py`

`project_loader.py` теперь не просто читает `demand_model`, а нормализует его.

```python
project = Project(
    project_name=data.get("project_name", "Unnamed Project"),
    pcu_coefficients=data.get("pcu_coefficients", {}),
    demand_model=self._normalize_demand_routes_in_model(data.get("demand_model", {})),
    metadata=data.get("metadata", {}),
)
```

Что происходит:

- `demand_model` загружается сразу в `Project`.
- Для routes внутри demand_model применяется нормализация.

```python
def _normalize_demand_route(self, route: dict) -> dict:
    normalized = dict(route)
    demand_value = self._route_demand_value(normalized)
    if demand_value is not None:
        normalized["demand_value"] = demand_value
    normalized.pop("demand_veh_h", None)
    normalized.pop("demand", None)
    normalized.pop("assigned_link_ids", None)
    normalized.pop("results", None)
    return normalized
```

Что происходит:

- Старые имена demand-полей приводятся к `demand_value`.
- Runtime-поля удаляются.
- Это снижает риск, что старые данные сломают assignment.

## 12. Важные связи с `project_saver.py`

`project_saver.py` сохраняет demand_model, но чистит runtime-поля.

```python
"demand_model": self._serialize_demand_model(project.demand_model),
```

Что происходит:

- Demand-модель входит в JSON проекта.

```python
def _serialize_link_traffic_counts(self, project: Project, link: Link) -> dict:
    if (
        project.demand_model
        and link.metadata.get("traffic_counts_source") == "assigned_demand"
    ):
        return dict(link.metadata.get("observed_traffic_counts", {}))
    return link.traffic_counts
```

Что происходит:

- Если traffic_counts были временно назначены demand assignment-ом, saver сохраняет исходные observed counts.
- Это не дает demand-результату перезаписать базовые данные проекта.

```python
if metadata.get("traffic_counts_source") == "assigned_demand":
    metadata.pop("traffic_counts_source", None)
    metadata.pop("observed_traffic_counts", None)
```

Что происходит:

- Runtime metadata demand assignment-а не сохраняется как постоянная часть проекта.

## 13. Важные связи с `analysis_service.py`

`AnalysisService` теперь умеет работать с demand-моделью.

```python
def __init__(self) -> None:
    self.validation_service = ValidationService()
    self.demand_assignment_service = DemandAssignmentService()
```

Что происходит:

- Анализатор создает сервис валидации и demand assignment.

```python
if assign_demand and self._has_demand_model(project):
    validation_errors = self.validation_service.validate_project(project)
    if validation_errors:
        self._mark_analysis_failed(project, "Validation failed")
        return {...}

    assignment_report = self.demand_assignment_service.assign(project)
    if not assignment_report.get("success"):
        self._mark_analysis_failed(project, "Demand assignment failed")
        return {...}
```

Что происходит:

- Перед demand assignment выполняется валидация проекта.
- Если валидация или assignment провалились, links получают статус `UNDEFINED`, а анализ не продолжается.

```python
links_report = self._analyze_links(project)
routes_report = self._analyze_visual_routes(project)
demand_routes_report = self._analyze_demand_routes(project, assignment_report)
```

Что происходит:

- После успешного assignment-а считается обычная link-аналитика.
- Отдельно считаются:
  - визуальные маршруты из `network.routes`;
  - demand routes из отчета assignment.

## 14. Критический порядок выполнения

Правильный end-to-end процесс:

```text
1. osm_project_importer.py
   OSM -> Project JSON

2. enrich_project_with_skdf.py
   Project JSON + SKDF CSV -> enriched Project JSON

3. traffic_viz.py или demand_model_wizard.py
   enriched Project JSON -> project.demand_model

4. AnalysisService
   demand_model -> assigned link.traffic_counts -> link.results
```

Если поменять порядок:

- Если сначала сгенерировать demand, а потом запускать SKDF enrichment, СКДФ может перезаписать traffic/capacity.
- Если запустить analysis без demand_model, расчет пойдет по текущим `link.traffic_counts`.
- Если `project_saver.py` сохраняет проект после assignment, он старается не записывать временные assigned-demand потоки как исходные.

## 15. Что является постоянными данными, а что runtime-данными

Постоянные данные:

- `Project.network.nodes`
- `Project.network.links`
- `link.coords`
- `link.parameters`
- `link.metadata["skdf"]`
- `Project.demand_model`
- `Project.metadata["skdf_enrichment"]`

Runtime-данные:

- `link.results`
- `route.results`
- `link.metadata["traffic_counts_source"] == "assigned_demand"`
- `link.metadata["observed_traffic_counts"]`
- `Demand_Assignment` в report-е `AnalysisService`

Практический смысл:

- Runtime-данные можно пересчитать.
- Постоянные данные должны сохраняться аккуратно, потому что они задают исходную модель.

## 16. Краткая карта ответственности

```text
osm_project_importer.py
  Строит граф из OSM.

skdf_matcher.py
  Ищет соответствия OSM link <-> SKDF road и переносит параметры.

enrich_project_with_skdf.py
  CLI-обертка для загрузки проекта, запуска matcher-а и сохранения результата.

demand_model_utils.py
  Общие проверки demand-маршрутов и числовых полей.

demand_assignment_service.py
  Превращает demand_model в traffic_counts по links.

demand_model_wizard.py
  Автоматически создает черновую demand_model и сохраняет ее в JSON.

traffic_viz.py
  Загружает проект, запускает анализ, показывает граф, demand-summary, маршруты и детали nodes/links.
```

## 17. Основные риски и места, на которые стоит смотреть

1. `osm_project_importer.py`: `ALLOWED_HIGHWAYS` сейчас исключает `residential`. Это уменьшает граф, но может отрезать локальные связи.

2. `skdf_matcher.py`: качество enrichment зависит от геометрии и названий. Отчет `skdf_match_report.csv` обязателен для проверки спорных случаев.

3. `demand_model_wizard.py`: автогенерация demand-модели создает черновик, не реальные потоки. Это прямо зафиксировано в metadata.

4. `demand_assignment_service.py`: assignment перезаписывает `link.traffic_counts` в runtime. После этого старые `link.results` сбрасываются.

5. `traffic_viz.py`: при наличии `demand_model` анализ запускается всегда при загрузке проекта. Это нужно, но может менять отображаемые потоки относительно исходных SKDF/observed counts.

6. `project_saver.py`: сохраняет observed counts вместо assigned-demand counts. Это правильная защита, но если нужно зафиксировать именно assigned-demand как новый сценарий, потребуется отдельная логика экспорта.

## 18. Мини-сценарии использования

### Импорт OSM

```powershell
python osm_project_importer.py --input map_bez.osm --output osm_network_project_map_bez.json --intensity 600
```

Результат: JSON с графом.

### Обогащение СКДФ

```powershell
python enrich_project_with_skdf.py --project osm_network_project_map_bez.json --skdf-csv nsk_roads_bbox.csv --output osm_network_project_skdf_map_bez.json --report skdf_match_report.csv
```

Результат:

- обогащенный JSON;
- CSV-отчет сопоставления.

### Demand-модель через GUI

```text
traffic_viz.py
  -> кнопка "Автогенерация demand_model"
  -> DemandModelWizard
  -> сохранить в JSON
  -> reload_project_and_redraw()
```

Результат:

- `project.demand_model` в JSON;
- при следующем анализе demand assignment назначит потоки на links.

## 19. Итоговая зависимость данных

```text
OSM geometry
  -> Link.coords
  -> skdf_matcher geometry matching
  -> link.metadata["skdf"]
  -> link.parameters / link.traffic_counts

Demand model
  -> DemandAssignmentService
  -> runtime link.traffic_counts
  -> AnalysisService
  -> link.results
  -> traffic_viz colors and reports
```

Ключевая идея: OSM задает топологию и геометрию, SKDF добавляет наблюдаемые дорожные параметры, demand-модель может заменить текущие потоки сценарными потоками, а `AnalysisService` пересчитывает транспортные показатели поверх текущего состояния `Project`.
