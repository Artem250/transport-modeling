import os
import sys
import xml.etree.ElementTree as ET
import math
from html import escape

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QBrush, QFont, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent, QPainterPathStroker
from PyQt5.QtWidgets import (
    QApplication, QGraphicsEllipseItem, QGraphicsItem, QGraphicsPathItem,
    QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QMainWindow,
    QMessageBox, QPushButton, QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QSlider
)

from project_loader import ProjectLoader
from project_saver import ProjectSaver

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineProfile, QWebEngineView
except ImportError:
    QWebEngineProfile = None
    QWebEngineView = None

try:
    from pyproj import Transformer

    transformer = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)
    inv_transformer = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)
    USE_PYPROJ = True
except ImportError:
    USE_PYPROJ = False


def project_coords(lon, lat):
    if USE_PYPROJ:
        x, y = transformer.transform(lon, lat)
        return x, -y
    r_major = 6378137.0
    x = r_major * math.radians(lon)
    scale = x / lon if lon != 0 else 1
    y = 180.0 / math.pi * math.log(math.tan(math.pi / 4.0 + lat * (math.pi / 180.0) / 2.0)) * scale
    return x, -y


def unproject_coords(x, y_qt):
    if USE_PYPROJ:
        lon, lat = inv_transformer.transform(x, -y_qt)
        return lon, lat
    r_major = 6378137.0
    lon = math.degrees(x / r_major)
    lat = math.degrees(2 * math.atan(math.exp(math.radians(-y_qt))) - math.pi / 2)
    return lon, lat


# --- АКАДЕМИЧЕСКАЯ ТАБЛИЦА ПЛОТНОСТЕЙ ДЛЯ LOS (Легковые на км на полосу) ---
def get_los_and_color_from_density(density_per_lane):
    if density_per_lane <= 11: return "A", QColor(0, 200, 0)  # Свободный поток
    if density_per_lane <= 16: return "B", QColor(100, 220, 100)  # Стабильный поток
    if density_per_lane <= 22: return "C", QColor(255, 255, 0)  # Плотный поток
    if density_per_lane <= 28: return "D", QColor(255, 165, 0)  # Предзаторовое состояние
    if density_per_lane <= 35: return "E", QColor(255, 69, 0)  # Критическая плотность (Capacity)
    return "F", QColor(255, 0, 0)  # Затор / Пробка


NODE_COLORS = {
    "boundary": QColor(220, 40, 40),
    "intersection": QColor(45, 90, 210),
    "roundabout_part": QColor(150, 70, 210),
    "ordinary": QColor(80, 80, 80),
}


# Функция нарезки ломаной линии на N равных частей (ячеек CTM)
def split_polyline(points, num_segments):
    if num_segments <= 1 or len(points) < 2:
        return [points]

    lengths = []
    for i in range(len(points) - 1):
        dx = points[i + 1].x() - points[i].x()
        dy = points[i + 1].y() - points[i].y()
        lengths.append(math.hypot(dx, dy))

    total_len = sum(lengths)
    if total_len == 0:
        return [points] * num_segments

    target = total_len / num_segments
    cells = []
    curr_cell = [points[0]]

    pt_idx = 0
    p1 = points[0]

    for _ in range(num_segments - 1):
        dist_needed = target
        while dist_needed > 1e-5 and pt_idx < len(points) - 1:
            p2 = points[pt_idx + 1]
            seg_len = math.hypot(p2.x() - p1.x(), p2.y() - p1.y())

            if seg_len > dist_needed:
                t = dist_needed / seg_len
                nx = p1.x() + t * (p2.x() - p1.x())
                ny = p1.y() + t * (p2.y() - p1.y())
                new_pt = QPointF(nx, ny)
                curr_cell.append(new_pt)
                cells.append(curr_cell)
                curr_cell = [new_pt]
                p1 = new_pt
                dist_needed = 0
            else:
                curr_cell.append(p2)
                dist_needed -= seg_len
                pt_idx += 1
                p1 = points[pt_idx]

        if dist_needed > 1e-5:
            cells.append(curr_cell)
            curr_cell = [p1]

    while pt_idx < len(points) - 1:
        curr_cell.append(points[pt_idx + 1])
        pt_idx += 1
    cells.append(curr_cell)

    while len(cells) < num_segments:
        cells.append([points[-1], points[-1]])

    return cells


class MapBackgroundItem(QGraphicsItem):
    def __init__(self, map_data):
        super().__init__()
        self.roads = map_data.get("roads", [])
        self.buildings = map_data.get("buildings", [])
        self.building_pen = QPen(QColor(190, 190, 190), 0.8)
        self.building_brush = QBrush(QColor(235, 235, 235))
        self.road_styles = {
            "primary": QPen(QColor(170, 170, 170), 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin),
            "secondary": QPen(QColor(185, 185, 185), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin),
            "tertiary": QPen(QColor(200, 200, 200), 2.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin),
            "residential": QPen(QColor(215, 215, 215), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin),
            "service": QPen(QColor(225, 225, 225), 1, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin),
            "unclassified": QPen(QColor(210, 210, 210), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin),
        }

        all_points = []
        for road in self.roads:
            all_points.extend(road["coords"])
        for building in self.buildings:
            all_points.extend(building["coords"])

        if not all_points:
            self.rect = QRectF(0, 0, 100, 100)
        else:
            all_x = [p[0] for p in all_points]
            all_y = [p[1] for p in all_points]
            self.rect = QRectF(min(all_x), min(all_y), max(all_x) - min(all_x), max(all_y) - min(all_y))
        self.setZValue(-100)

    def boundingRect(self):
        return self.rect

    def paint(self, painter, option, widget):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(self.building_pen)
        painter.setBrush(self.building_brush)
        for building in self.buildings:
            points = building["coords"]
            if len(points) >= 3:
                painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]))

        painter.setBrush(Qt.NoBrush)
        for road in self.roads:
            points = road["coords"]
            pen = self.road_styles.get(
                road["type"],
                QPen(QColor(210, 210, 210), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin),
            )
            painter.setPen(pen)
            if len(points) > 1:
                painter.drawPolyline(QPolygonF([QPointF(x, y) for x, y in points]))


class TrafficNode(QGraphicsEllipseItem):
    def __init__(self, node_model, label, pos_point, app_callback=None):
        radius = 6
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.node_model = node_model
        self.label = label
        self.app_callback = app_callback
        self.setPos(pos_point)
        self.setBrush(QBrush(NODE_COLORS.get(node_model.node_type, NODE_COLORS["ordinary"])))
        self.setPen(QPen(Qt.black, 1))
        self.setZValue(100)  # Поверх дорог
        self.setCacheMode(QGraphicsItem.NoCache)
        self.setFlags(
            QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsScenePositionChanges)
        self.connected_links = []

    def add_link(self, link):
        if link not in self.connected_links:
            self.connected_links.append(link)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            for link in self.connected_links:
                link.update_geometry()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.app_callback:
            self.app_callback(self.node_model)

    def boundingRect(self):
        return super().boundingRect().adjusted(-5, -2, 5, 5)

    def paint(self, painter, option, widget):
        super().paint(painter, option, widget)
        font = QFont("Arial", 1)
        painter.setFont(font)
        painter.setPen(Qt.black)
        text_rect = QRectF(-40, self.rect().bottom() + 1, 80, 3)
        painter.drawText(text_rect, Qt.AlignCenter, self.label)


class TrafficLink(QGraphicsPathItem):
    def __init__(self, link_model, start_node, end_node, app_callback=None):
        super().__init__()
        self.link_model = link_model
        self.id = link_model.id
        self.start_node = start_node
        self.end_node = end_node
        self.app_callback = app_callback
        self.intermediate_points = []
        self.shape_handles = []
        self.visual_offset_px = 0.0

        self.time_index = 0
        self.cell_points = []  # Массив массивов координат для ячеек

        coords = link_model.coords or {}
        if coords.get("type") == "polyline":
            for p in coords.get("points", [])[1:-1]:
                self.intermediate_points.append(QPointF(*project_coords(p[0], p[1])))

        self.start_node.add_link(self)
        self.end_node.add_link(self)
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setZValue(50)
        self.setCacheMode(QGraphicsItem.NoCache)
        self.base_width = 8
        self.update_geometry()

    def update_geometry(self):
        self.prepareGeometryChange()

        base_points = [self.start_node.scenePos()]
        for pt in self.intermediate_points:
            base_points.append(QPointF(pt))
        base_points.append(self.end_node.scenePos())

        OFFSET_PX = self.visual_offset_px
        shifted_points = []
        for i in range(len(base_points)):
            if i == 0:
                dx, dy = base_points[1].x() - base_points[0].x(), base_points[1].y() - base_points[0].y()
            elif i == len(base_points) - 1:
                dx, dy = base_points[-1].x() - base_points[-2].x(), base_points[-1].y() - base_points[-2].y()
            else:
                dx, dy = base_points[i + 1].x() - base_points[i - 1].x(), base_points[i + 1].y() - base_points[
                    i - 1].y()

            length = math.hypot(dx, dy)
            if length == 0:
                shifted_points.append(base_points[i])
                continue
            nx, ny = -dy / length, dx / length
            shifted_points.append(QPointF(base_points[i].x() + nx * OFFSET_PX, base_points[i].y() + ny * OFFSET_PX))

        # Устанавливаем общий путь для хитбокса (выделения мышкой)
        full_path = QPainterPath()
        full_path.moveTo(shifted_points[0])
        for pt in shifted_points[1:]: full_path.lineTo(pt)
        self.setPath(full_path)

        # Нарезаем линию на ячейки CTM
        res = self.link_model.results or {}
        cell_count = res.get("cell_count", 1)
        self.cell_points = split_polyline(shifted_points, cell_count)

    def boundingRect(self):
        margin = self.base_width * 4 + 20
        return super().boundingRect().adjusted(-margin, -margin, margin, margin)

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(self.base_width + 10)
        stroker.setCapStyle(Qt.RoundCap)
        stroker.setJoinStyle(Qt.RoundJoin)
        return stroker.createStroke(self.path())

    def update_visuals(self, time_idx):
        self.time_index = time_idx
        self.update()  # Запрашиваем перерисовку

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.app_callback:
            self.app_callback(self.link_model)

    def paint(self, painter, option, widget):
        # ВАЖНО: Мы переопределяем paint, чтобы рисовать ячейки вместо сплошной линии
        res = self.link_model.results or {}
        hist_dens = res.get("history_cells_density_pcu_km", [])
        lanes = self.link_model.parameters.get("lanes_total", 1)

        # Получаем плотности для текущего кадра времени
        if hist_dens and self.time_index < len(hist_dens):
            current_densities = hist_dens[self.time_index]
        else:
            current_densities = [0] * len(self.cell_points)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)

        last_color = QColor(200, 200, 200)

        # Отрисовка каждой ячейки своим цветом
        for i, cell_pts in enumerate(self.cell_points):
            if i < len(current_densities):
                d = current_densities[i]
            else:
                d = 0

            _, color = get_los_and_color_from_density(d / lanes)
            if self.isSelected():
                # Подсветка выделенной дороги синим контуром
                painter.setPen(QPen(QColor(0, 170, 255), self.base_width + 4, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                path = QPainterPath()
                path.moveTo(cell_pts[0])
                for pt in cell_pts[1:]: path.lineTo(pt)
                painter.drawPath(path)

            painter.setPen(QPen(color, self.base_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

            path = QPainterPath()
            path.moveTo(cell_pts[0])
            for pt in cell_pts[1:]: path.lineTo(pt)
            painter.drawPath(path)
            last_color = color

        # Отрисовка стрелки направления на конце дороги
        if len(self.cell_points) > 0:
            last_cell = self.cell_points[-1]
            if len(last_cell) >= 2:
                p1, p2 = last_cell[-2], last_cell[-1]
                dx, dy = p2.x() - p1.x(), p2.y() - p1.y()
                if math.hypot(dx, dy) > 0:
                    angle = math.degrees(math.atan2(-dy, dx))

                    arrow_color = QColor(last_color)
                    arrow_color.setAlpha(255)
                    painter.setBrush(QBrush(arrow_color))
                    painter.setPen(Qt.NoPen)

                    painter.translate(p2)
                    painter.rotate(-angle)

                    arrow_size = self.base_width * 1.3
                    arrow_head = QPolygonF([
                        QPointF(arrow_size, 0),
                        QPointF(-arrow_size, -arrow_size * 0.6),
                        QPointF(-arrow_size, arrow_size * 0.6)
                    ])
                    painter.drawPolygon(arrow_head)

        painter.restore()


class ShapePointHandle(QGraphicsEllipseItem):
    def __init__(self, bindings, pos_point):
        radius = 5.0
        super().__init__(-radius, -radius, radius * 2.0, radius * 2.0)
        self.bindings = bindings
        self.setPos(QPointF(pos_point))
        self.setBrush(QBrush(QColor(255, 255, 255)))
        self.setPen(QPen(QColor(35, 35, 35), 1.2))
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setZValue(90)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and hasattr(self, "bindings"):
            updated_links = {}
            for link_item, point_index in self.bindings:
                link_item.intermediate_points[point_index] = QPointF(value)
                updated_links[id(link_item)] = link_item
            for link_item in updated_links.values():
                link_item.update_geometry()
        return super().itemChange(change, value)


class MapViewer(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15
        self.scale(factor, factor) if event.angleDelta().y() > 0 else self.scale(1 / factor, 1 / factor)


class MainWindow(QMainWindow):
    def __init__(self, map_file="map_nstu.osm", data_file="ctm_results_viz.json"):
        super().__init__()
        self.setWindowTitle("Транспортный визуализатор CTM")
        self.resize(1400, 900)

        self.loader = ProjectLoader()
        self.saver = ProjectSaver()
        self.data_file = data_file
        self.project = None
        self.map_data = self.parse_osm(map_file)
        self.viz_links = []
        self.current_time_index = 0
        self.last_clicked_link = None
        self.last_clicked_node = None

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # Левая часть (карта и ползунок)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)

        self.scene = QGraphicsScene()
        self.view = MapViewer(self.scene)
        left_layout.addWidget(self.view)

        # Ползунок времени
        slider_layout = QHBoxLayout()
        self.lbl_time = QLabel("Время: 0 мин")
        self.lbl_time.setFixedWidth(120)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(0)
        self.slider.valueChanged.connect(self.on_time_changed)

        slider_layout.addWidget(self.lbl_time)
        slider_layout.addWidget(self.slider)
        left_layout.addLayout(slider_layout)

        main_layout.addWidget(left_panel, 4)

        # Правая часть (инфо и кнопки)
        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)
        main_layout.addWidget(control_panel, 1)

        self.info = QTextEdit()
        self.info.setReadOnly(True)
        control_layout.addWidget(QLabel("Детали объекта:"))
        control_layout.addWidget(self.info)

        self.btn_save_coords = QPushButton("Сохранить координаты в проект")
        self.btn_save_coords.clicked.connect(self.save_current_positions_to_project)
        control_layout.addWidget(self.btn_save_coords)

        self.reload_project_and_redraw()

    def snapshot_interval_sec(self):
        if not self.project:
            return 60
        config = self.project.metadata.get("ctm_scenario_config", {}) or {}
        return int(config.get("snapshot_interval_sec", 60) or 60)

    def current_time_min(self) -> float:
        return self.current_time_index * self.snapshot_interval_sec() / 60.0

    def format_time_min(self) -> str:
        minutes = self.current_time_min()
        if abs(minutes - round(minutes)) < 1e-9:
            return str(int(round(minutes)))
        return f"{minutes:.1f}"

    def on_time_changed(self, value):
        self.current_time_index = value

        for link in self.viz_links:
            link.update_visuals(self.current_time_index)

        self.lbl_time.setText(f"Время: {self.format_time_min()} мин")

        if self.last_clicked_link:
            self.on_link_click(self.last_clicked_link)
        elif self.last_clicked_node:
            self.on_node_click(self.last_clicked_node)

    def reload_project_and_redraw(self):
        self.scene.clear()
        self.viz_links = []
        self.last_clicked_link = None
        self.last_clicked_node = None
        self.draw_map()

        try:
            self.project = self.loader.load(self.data_file)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить результаты CTM: {exc}")
            self.project = None
            return

        self.draw_network()

        # Настраиваем ползунок времени (ищем максимальную длину истории)
        max_history = 0
        for link in self.project.network.links.values():
            res = link.results or {}
            hist = res.get("history_cells_density_pcu_km", [])
            if len(hist) > max_history:
                max_history = len(hist)

        if max_history > 0:
            self.slider.setMaximum(max_history - 1)
            self.slider.setValue(0)

        self.on_time_changed(self.slider.value())

    def parse_osm(self, path):
        try:
            tree = ET.parse(path)
            nodes = {}
            for n in tree.findall(".//node"):
                nodes[n.get("id")] = project_coords(float(n.get("lon")), float(n.get("lat")))
            roads = []
            buildings = []
            allowed_highways = {"primary", "secondary", "tertiary", "residential", "trunk", "service"}
            for w in tree.findall(".//way"):
                tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
                coords = [nodes[nd.get("ref")] for nd in w.findall("nd") if nd.get("ref") in nodes]
                if len(coords) < 2: continue
                if tags.get("highway") in allowed_highways:
                    roads.append({"type": tags["highway"], "coords": coords})
                if tags.get("building") is not None and len(coords) >= 3:
                    buildings.append({"type": tags["building"], "coords": coords})
            return {"roads": roads, "buildings": buildings}
        except Exception as exc:
            return {"roads": [], "buildings": []}

    def draw_map(self):
        self.scene.addItem(MapBackgroundItem(self.map_data))

    def draw_network(self):
        if self.project is None: return
        node_registry = {}
        for node_model in self.project.network.nodes.values():
            if node_model.lon is not None and node_model.lat is not None:
                point = QPointF(*project_coords(node_model.lon, node_model.lat))
            else:
                continue
            node_label = node_model.name or node_model.id
            node_item = TrafficNode(node_model, node_label, point, self.on_node_click)
            self.scene.addItem(node_item)
            node_registry[node_model.id] = node_item

        for link_model in self.project.network.links.values():
            start_node = node_registry.get(link_model.start_node_id)
            end_node = node_registry.get(link_model.end_node_id)
            if start_node is None or end_node is None: continue
            link_item = TrafficLink(link_model, start_node, end_node, self.on_link_click)
            self.scene.addItem(link_item)
            self.viz_links.append(link_item)

        self.apply_link_offsets()
        self.create_shared_shape_handles()

        if self.viz_links:
            self.view.fitInView(self.scene.itemsBoundingRect(), Qt.KeepAspectRatio)

    def apply_link_offsets(self):
        geometry_keys = {
            link: self.link_geometry_key(link.link_model)
            for link in self.viz_links
        }
        key_counts = {}
        for key in geometry_keys.values():
            if key:
                key_counts[key] = key_counts.get(key, 0) + 1

        for link, key in geometry_keys.items():
            has_reverse_geometry = bool(key and tuple(reversed(key)) in key_counts)
            link.visual_offset_px = 4.0 if has_reverse_geometry else 0.0
            link.update_geometry()

    def create_shared_shape_handles(self):
        handle_specs = {}
        for link in self.viz_links:
            link.shape_handles = []
            coords = link.link_model.coords or {}
            if coords.get("type") != "polyline":
                continue
            points = coords.get("points", [])
            for point_index, lon_lat in enumerate(points[1:-1]):
                key = self.lon_lat_key(lon_lat)
                if key not in handle_specs:
                    handle_specs[key] = {
                        "pos": QPointF(link.intermediate_points[point_index]),
                        "bindings": [],
                    }
                handle_specs[key]["bindings"].append((link, point_index))

        for spec in handle_specs.values():
            handle = ShapePointHandle(spec["bindings"], spec["pos"])
            self.scene.addItem(handle)
            for link, _ in spec["bindings"]:
                link.shape_handles.append(handle)

    def link_geometry_key(self, link_model):
        coords = link_model.coords or {}
        points = coords.get("points", [])
        if len(points) >= 2:
            return tuple(self.lon_lat_key(point) for point in points)

        lon_start = coords.get("lon_start")
        lat_start = coords.get("lat_start")
        lon_end = coords.get("lon_end")
        lat_end = coords.get("lat_end")
        if None in (lon_start, lat_start, lon_end, lat_end):
            return None
        return (
            self.lon_lat_key((lon_start, lat_start)),
            self.lon_lat_key((lon_end, lat_end)),
        )

    def lon_lat_key(self, lon_lat):
        return (round(float(lon_lat[0]), 6), round(float(lon_lat[1]), 6))

    def format_number(self, value, digits=1, suffix=""):
        if value is None or value == "":
            return "н/д"
        try:
            return f"{float(value):.{digits}f}{suffix}"
        except (TypeError, ValueError):
            return escape(str(value))

    def link_label(self, link):
        name = link.name or link.id
        return f"{escape(link.id)} ({escape(name)})"

    def list_link_labels(self, links):
        if not links:
            return "нет"
        return "<br>".join(self.link_label(link) for link in links)

    def link_fd_values(self, link_model):
        res = link_model.results or {}
        fd = res.get("fundamental_diagram") or {}
        metadata_links = (
            self.project.metadata
            .get("ctm_fundamental_diagram_model", {})
            .get("links", {})
            if self.project else {}
        )
        if not fd:
            fd = metadata_links.get(link_model.id, {}) or {}

        capacity = fd.get("capacity_pcu_h")
        critical_density = fd.get("critical_density_pcu_km")
        jam_density = fd.get("jam_density_pcu_km")
        if capacity is not None and critical_density is not None and jam_density is not None:
            return float(capacity), float(critical_density), float(jam_density)

        config = self.project.metadata.get("ctm_scenario_config", {}) if self.project else {}
        highway_params = config.get("highway_params", {}) or {}
        highway = link_model.metadata.get("highway", "default")
        params = highway_params.get(highway, highway_params.get("default", {})) or {}
        lanes = float(link_model.parameters.get("lanes_total", 1) or 1)
        speed = float(params.get("speed_kph", 0.0) or 0.0)
        cap_per_lane = float(params.get("cap_per_lane", 0.0) or 0.0)
        jam_per_lane = float(config.get("jam_density_pcu_km_per_lane", 140.0) or 140.0)
        capacity = cap_per_lane * lanes if cap_per_lane > 0.0 else None
        critical_density = capacity / speed if capacity is not None and speed > 0.0 else None
        jam_density = jam_per_lane * lanes
        return capacity, critical_density, jam_density

    def link_incident_data(self, link_model):
        incident = dict((link_model.results or {}).get("incident", {}) or {})
        project_incident = self.project.metadata.get("ctm_incident", {}) if self.project else {}
        if project_incident.get("link_id") == link_model.id:
            merged = dict(project_incident)
            merged.update(incident)
            incident = merged
        return incident

    def movement_current_flow(self, movement):
        history = movement.get("history_flow_veh_h", []) or []
        if self.current_time_index < len(history):
            return history[self.current_time_index]
        return None

    def movements_for_node(self, node_id):
        movements = self.project.metadata.get("ctm_movements", []) if self.project else []
        return [movement for movement in movements if movement.get("node_id") == node_id]

    def build_node_details_html(self, node_model):
        incoming = self.project.network.get_incoming_links(node_model.id)
        outgoing = self.project.network.get_outgoing_links(node_model.id)
        movements = self.movements_for_node(node_model.id)
        node_solver_cases = sorted(
            {
                str(movement.get("node_solver_case"))
                for movement in movements
                if movement.get("node_solver_case")
            }
        )
        node_solver = self.project.metadata.get("node_solver", "н/д") if self.project else "н/д"
        case_text = ", ".join(node_solver_cases) if node_solver_cases else escape(str(node_solver))

        html = f"<h3>Node: {escape(node_model.name or node_model.id)}</h3>"
        html += f"<b>ID:</b> {escape(node_model.id)}<br>"
        html += f"<b>Type:</b> {escape(str(node_model.node_type))}<br>"
        html += f"<b>Time:</b> {self.format_time_min()} min<br><br>"
        html += f"<b>Incoming links ({len(incoming)}):</b><br>{self.list_link_labels(incoming)}<br><br>"
        html += f"<b>Outgoing links ({len(outgoing)}):</b><br>{self.list_link_labels(outgoing)}<br><br>"
        html += f"<b>Node solver case:</b> {escape(case_text)}<br>"

        if not movements:
            html += "<br><b>Movements:</b> нет данных<br>"
            return html

        html += """
        <br><b>Movements table:</b>
        <table border="1" cellspacing="0" cellpadding="3">
        <tr>
            <th>in -> out</th>
            <th>turn_type</th>
            <th>ratio</th>
            <th>avg/max/current flow, veh/h</th>
            <th>active constraints</th>
            <!-- <th>source / reason</th> -->
        </tr>
        """
        for movement in sorted(movements, key=lambda item: (item.get("in_link_id", ""), item.get("out_link_id", ""))):
            in_id = escape(str(movement.get("in_link_id", "")))
            out_id = escape(str(movement.get("out_link_id", "")))
            constraints = movement.get("active_constraints", []) or []
            # reason = movement.get("reason", []) or []
            flow_text = (
                f"{self.format_number(movement.get('avg_flow_veh_h'), 1)} / "
                f"{self.format_number(movement.get('max_flow_veh_h'), 1)} / "
                f"{self.format_number(self.movement_current_flow(movement), 1)}"
            )
            html += "<tr>"
            html += f"<td>{in_id} -> {out_id}</td>"
            html += f"<td>{escape(str(movement.get('turn_type', '')))}</td>"
            html += f"<td>{self.format_number(movement.get('turn_ratio'), 3)}</td>"
            html += f"<td>{flow_text}</td>"
            html += f"<td>{escape(', '.join(map(str, constraints)) or 'нет')}</td>"
            # html += (
            #     f"<td>{escape(str(movement.get('source', 'н/д')))}"
            #     f"<br>{escape(', '.join(map(str, reason)) or 'нет')}</td>"
            # )
            html += "</tr>"
        html += "</table>"
        return html

    def build_link_details_html(self, link_model):
        res = link_model.results or {}
        hist_dens = res.get("history_cells_density_pcu_km", []) or []
        hist_flow = res.get("history_flow_veh_h", []) or []
        lanes = float(link_model.parameters.get("lanes_total", 1) or 1)

        if hist_dens and self.current_time_index < len(hist_dens):
            current_densities = hist_dens[self.current_time_index]
            avg_dens = sum(current_densities) / len(current_densities)
            max_dens = max(current_densities)
            current_flow = hist_flow[self.current_time_index] if self.current_time_index < len(hist_flow) else None
            los_avg, _ = get_los_and_color_from_density(avg_dens / lanes)
            los_max, _ = get_los_and_color_from_density(max_dens / lanes)
        else:
            avg_dens, max_dens, current_flow = None, None, None
            los_avg, los_max = "н/д", "н/д"

        capacity, critical_density, jam_density = self.link_fd_values(link_model)
        flow_to_capacity = (
            float(current_flow) / capacity
            if current_flow is not None and capacity not in (None, 0.0)
            else None
        )
        incident = self.link_incident_data(link_model)
        capacity_factor = incident.get("capacity_factor")
        effective_incident_capacity = (
            capacity * float(capacity_factor)
            if capacity is not None and capacity_factor not in (None, "")
            else None
        )
        incident_start = incident.get("start_time_sec")
        incident_end = incident.get("end_time_sec")
        incident_active = "н/д"
        if incident_start is not None and incident_end is not None:
            t_sec = self.current_time_min() * 60.0
            incident_active = "yes" if float(incident_start) <= t_sec < float(incident_end) else "no"

        source_inflows = self.project.metadata.get("ctm_source_inflows_veh_h", {}) if self.project else {}
        source_inflow = source_inflows.get(link_model.id)

        html = f"<h3>Link: {escape(link_model.name or link_model.id)}</h3>"
        html += f"<b>ID:</b> {escape(link_model.id)}<br>"
        html += f"<b>From -> to:</b> {escape(link_model.start_node_id)} -> {escape(link_model.end_node_id)}<br>"
        html += f"<b>Length:</b> {self.format_number(link_model.length_km, 3)} km<br>"
        html += f"<b>Lanes:</b> {self.format_number(lanes, 0)}<br>"
        html += f"<b>OSM highway:</b> {escape(str(link_model.metadata.get('highway', 'н/д')))}<br>"
        html += f"<b>Time:</b> {self.format_time_min()} min<br><br>"

        html += "<b>CTM / FD:</b><br>"
        html += f"Capacity: {self.format_number(capacity, 1)} veh/h<br>"
        html += f"Critical density: {self.format_number(critical_density, 1)} pcu/km<br>"
        html += f"Jam density: {self.format_number(jam_density, 1)} pcu/km<br>"
        html += f"Current flow / capacity: {self.format_number(flow_to_capacity, 3)}<br>"
        if source_inflow is not None:
            html += f"Source inflow: {self.format_number(source_inflow, 1)} veh/h<br>"

        html += "<br><b>Incident:</b><br>"
        html += f"Incident model: {escape(str(incident.get('incident_model', 'нет')))}<br>"
        html += f"Blocked lanes: {escape(str(incident.get('blocked_lanes', 'н/д')))}<br>"
        html += f"Capacity factor: {self.format_number(capacity_factor, 3)}<br>"
        html += f"Effective incident capacity: {self.format_number(effective_incident_capacity, 1)} veh/h<br>"
        html += f"Incident active now: {escape(incident_active)}<br>"

        html += f"<br><b>Dynamics CTM ({self.format_time_min()} min):</b><br>"
        html += f"<b>Output flow:</b> {self.format_number(current_flow, 1)} veh/h<br>"
        html += f"<b>Density avg:</b> {self.format_number(avg_dens, 1)} pcu/km (LOS {escape(str(los_avg))})<br>"
        html += f"<b>Density max cell:</b> {self.format_number(max_dens, 1)} pcu/km (LOS {escape(str(los_max))})<br>"
        return html

    def on_node_click(self, node_model):
        self.last_clicked_link = None
        self.last_clicked_node = node_model
        self.info.setHtml(self.build_node_details_html(node_model))
        return

    def on_link_click(self, link_model):
        self.last_clicked_link = link_model
        self.last_clicked_node = None
        self.info.setHtml(self.build_link_details_html(link_model))
        return

    def save_current_positions_to_project(self):
        if self.project is None: return
        updated_nodes = set()
        for link in self.viz_links:
            p1 = link.start_node.scenePos()
            p2 = link.end_node.scenePos()
            lon_s, lat_s = unproject_coords(p1.x(), p1.y())
            lon_e, lat_e = unproject_coords(p2.x(), p2.y())
            for node_item, lon, lat in (
                (link.start_node, lon_s, lat_s),
                (link.end_node, lon_e, lat_e),
            ):
                if node_item.id in updated_nodes:
                    continue
                node_item.node_model.lon = round(lon, 6)
                node_item.node_model.lat = round(lat, 6)
                updated_nodes.add(node_item.id)

            coords = link.link_model.coords or {}
            if coords.get("type") == "polyline" or link.intermediate_points:
                intermediate_points = []
                for point in link.intermediate_points:
                    lon_i, lat_i = unproject_coords(point.x(), point.y())
                    intermediate_points.append([round(lon_i, 6), round(lat_i, 6)])
                link.link_model.coords = {
                    "type": "polyline",
                    "points": [[round(lon_s, 6), round(lat_s, 6)], *intermediate_points, [round(lon_e, 6), round(lat_e, 6)]],
                    "lon_start": round(lon_s, 6),
                    "lat_start": round(lat_s, 6),
                    "lon_end": round(lon_e, 6),
                    "lat_end": round(lat_e, 6),
                }
            else:
                link.link_model.coords = {
                    "lon_start": round(lon_s, 6),
                    "lat_start": round(lat_s, 6),
                    "lon_end": round(lon_e, 6),
                    "lat_end": round(lat_e, 6),
                }
        try:
            self.saver.save(self.project, self.data_file)
            QMessageBox.information(self, "Успех", f"Координаты сохранены в {self.data_file}")
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить проект: {exc}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
