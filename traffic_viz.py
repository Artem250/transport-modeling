import os
import sys
import math
import xml.etree.ElementTree as ET

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QBrush, QFont, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent
from PyQt5.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from analysis_service import AnalysisService
from project_loader import ProjectLoader
from project_saver import ProjectSaver
from routing_service import RoutingService

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


LOS_COLORS = {
    "A": QColor(0, 200, 0),
    "B": QColor(100, 220, 100),
    "C": QColor(255, 255, 0),
    "D": QColor(255, 165, 0),
    "E": QColor(255, 69, 0),
    "F": QColor(255, 0, 0),
    "UNDEFINED": QColor(200, 200, 200),
}


class MapBackgroundItem(QGraphicsItem):
    def __init__(self, ways_data):
        super().__init__()
        self.ways = ways_data
        self.pen = QPen(QColor(220, 220, 220), 2)
        if not self.ways:
            self.rect = QRectF(0, 0, 100, 100)
        else:
            all_x = [p[0] for w in self.ways for p in w]
            all_y = [p[1] for w in self.ways for p in w]
            self.rect = QRectF(min(all_x), min(all_y), max(all_x) - min(all_x), max(all_y) - min(all_y))

    def boundingRect(self):
        return self.rect

    def paint(self, painter, option, widget):
        painter.setPen(self.pen)
        for way_points in self.ways:
            if len(way_points) > 1:
                poly = QPolygonF([QPointF(x, y) for x, y in way_points])
                painter.drawPolyline(poly)


class TrafficNode(QGraphicsEllipseItem):
    def __init__(self, node_id, label, pos_point):
        radius = 5
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.node_id = node_id
        self.label = label
        self.setPos(pos_point)
        self.setBrush(QBrush(QColor(50, 50, 150)))
        self.setPen(QPen(Qt.black, 1))
        self.setZValue(100)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsScenePositionChanges)
        self.connected_links = []

    def add_link(self, link):
        if link not in self.connected_links:
            self.connected_links.append(link)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            for link in self.connected_links:
                link.update_geometry()
        return super().itemChange(change, value)

    def paint(self, painter, option, widget):
        super().paint(painter, option, widget)
        font = QFont("Arial", 1)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(Qt.white)
        painter.drawText(self.rect(), Qt.AlignCenter, self.label)


class TrafficLink(QGraphicsPathItem):
    def __init__(self, link_model, start_node, end_node, app_callback=None):
        super().__init__()
        self.link_model = link_model
        self.id = link_model.id
        self.start_node = start_node
        self.end_node = end_node
        self.app_callback = app_callback
        self.is_route_highlighted = False
        self.intermediate_points = []
        res = link_model.results or {}
        self.is_ring = res.get("is_ring", False) or ("RING" in self.id and "CIRCULATION" in self.id)

        coords = link_model.coords or {}
        if not self.is_ring and coords.get("type") == "polyline":
            raw_points = coords.get("points", [])
            if len(raw_points) > 2:
                for p in raw_points[1:-1]:
                    self.intermediate_points.append(project_coords(p[0], p[1]))

        self.start_node.add_link(self)
        self.end_node.add_link(self)
        self.setFlags(QGraphicsItem.ItemIsSelectable)
        self.setZValue(50)
        self.base_width = 8
        self.update_geometry()

    def update_geometry(self):
        path = QPainterPath()
        p1 = self.start_node.scenePos()
        p2 = self.end_node.scenePos()

        if self.is_ring:
            vec = p2 - p1
            dist = math.sqrt(vec.x() ** 2 + vec.y() ** 2)
            if dist > 1.0:
                radius = dist / math.sqrt(2)
                mid = (p1 + p2) / 2
                perp_x = -(p2.y() - p1.y()) / dist
                perp_y = (p2.x() - p1.x()) / dist
                h = dist / 2
                center_x = mid.x() + perp_x * h
                center_y = mid.y() + perp_y * h
                rect = QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2)
                path.addEllipse(rect)
            else:
                path.moveTo(p1)
                path.lineTo(p2)
        else:
            path.moveTo(p1)
            for pt in self.intermediate_points:
                path.lineTo(QPointF(pt[0], pt[1]))
            path.lineTo(p2)
        self.setPath(path)

    def update_visuals(self, stage):
        res = self.link_model.results
        color = Qt.gray
        width = self.base_width

        if stage == 1:
            color = LOS_COLORS.get(res.get("LOS", "UNDEFINED"), Qt.gray)
        elif stage == 2:
            if res.get("Optimization_Proposal"):
                color = QColor(255, 0, 0)
                width = 12
            else:
                color = QColor(0, 200, 0, 80)
        elif stage == 3:
            delay = res.get("Delay_sec", 0)
            if delay > 60:
                color = QColor(200, 0, 0)
            elif delay > 10:
                color = QColor(200, 200, 0)
            else:
                color = QColor(200, 200, 200)
        elif stage == 4:
            color = QColor(180, 180, 180)
            if self.is_route_highlighted:
                color = QColor(0, 170, 255)
                width = 12

        self.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.app_callback:
            self.app_callback(self.link_model)


class MapViewer(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(factor, factor)
        else:
            self.scale(1 / factor, 1 / factor)


class MainWindow(QMainWindow):
    def __init__(self, map_file="map.osm", data_file="osm_network_project_skdf.json"):
        super().__init__()
        self.setWindowTitle("Транспортный визуализатор")
        self.resize(1400, 900)

        self.loader = ProjectLoader()
        self.saver = ProjectSaver()
        self.analysis_service = AnalysisService()
        self.routing_service = RoutingService()
        if not os.path.exists(data_file) and os.path.exists("manual_network.json"):
            data_file = "manual_network.json"
        self.data_file = data_file
        self.project = None
        self.map_data = self.parse_osm(map_file)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QHBoxLayout(central_widget)

        self.scene = QGraphicsScene()
        self.view = MapViewer(self.scene)
        self.map_stack = QStackedWidget()
        self.map_stack.addWidget(self.view)
        self.configure_webengine_profile()
        self.web_view = QWebEngineView() if QWebEngineView is not None else None
        if self.web_view is not None:
            self.map_stack.addWidget(self.web_view)
        layout.addWidget(self.map_stack, 4)

        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)
        layout.addWidget(control_panel, 1)

        group_box = QGroupBox("Режим визуализации")
        group_layout = QVBoxLayout()
        self.rb1 = QRadioButton("1. V/C и LOS")
        self.rb1.setChecked(True)
        self.rb2 = QRadioButton("2. Оптимизация")
        self.rb3 = QRadioButton("3. Задержки")
        self.rb4 = QRadioButton("4. Маршрут")
        self.rb1.toggled.connect(lambda: self.set_stage(1))
        self.rb2.toggled.connect(lambda: self.set_stage(2))
        self.rb3.toggled.connect(lambda: self.set_stage(3))
        self.rb4.toggled.connect(lambda: self.set_stage(4))
        for rb in (self.rb1, self.rb2, self.rb3, self.rb4):
            group_layout.addWidget(rb)
        group_box.setLayout(group_layout)
        control_layout.addWidget(group_box)

        self.info = QTextEdit()
        self.info.setReadOnly(True)
        control_layout.addWidget(QLabel("Детали объекта:"))
        control_layout.addWidget(self.info)

        self.btn_find_route = QPushButton("Найти маршрут")
        self.btn_find_route.clicked.connect(self.find_route)
        control_layout.addWidget(self.btn_find_route)

        self.btn_save_coords = QPushButton("Сохранить координаты в проект")
        self.btn_save_coords.clicked.connect(self.save_current_positions_to_project)
        control_layout.addWidget(self.btn_save_coords)

        self.btn_open_editor = QPushButton("Открыть редактор сети")
        self.btn_open_editor.clicked.connect(self.open_network_editor)
        control_layout.addWidget(self.btn_open_editor)

        self.btn_web_map = QPushButton("Открыть web-карту")
        self.btn_web_map.clicked.connect(self.open_folium_map)
        control_layout.addWidget(self.btn_web_map)

        self.current_stage = 1
        self.viz_links = []
        self.link_index = {}

        self.draw_map()
        self.load_project_data(data_file)
        self.draw_network()
        self.set_stage(1)

    def configure_webengine_profile(self):
        if QWebEngineProfile is None:
            return
        cache_dir = os.path.join(os.path.expanduser("~"), ".praktika_qtwebengine_cache")
        os.makedirs(cache_dir, exist_ok=True)
        profile = QWebEngineProfile.defaultProfile()
        profile.setCachePath(cache_dir)
        profile.setPersistentStoragePath(cache_dir)

    def open_network_editor(self):
        try:
            from network_editor import NetworkEditor
        except ImportError:
            from ne_network_editor import NetworkEditor

        self.editor_window = NetworkEditor(project_file=self.data_file)
        self.editor_window.show()

    def open_folium_map(self):
        if self.project is None:
            QMessageBox.warning(self, "Web map", "Проект не загружен.")
            return
        if QWebEngineView is None:
            QMessageBox.warning(
                self,
                "Web map",
                "Не удалось открыть web-карту:\n"
                "PyQtWebEngine не импортируется в Python, которым запущено приложение.\n"
                f"Python: {sys.executable}\n"
                f"Команда: \"{sys.executable}\" -m pip install PyQtWebEngine",
            )
            return

        if self.web_view is None:
            QMessageBox.warning(self, "Web map", "Web-компонент не создан.")
            return

        if self.map_stack.currentWidget() is self.web_view:
            self.map_stack.setCurrentWidget(self.view)
            self.btn_web_map.setText("Открыть web-карту")
            return

        try:
            from folium_map_viewer import build_project_map_html
        except Exception as e:
            QMessageBox.warning(
                self,
                "Web map",
                f"Не удалось открыть web-карту:\n{e}",
            )
            return

        try:
            self.web_view.setHtml(build_project_map_html(self.project))
            self.map_stack.setCurrentWidget(self.web_view)
            self.btn_web_map.setText("Вернуться к схеме")
        except Exception as e:
            QMessageBox.warning(self, "Web map", f"Не удалось построить web-карту:\n{e}")

    def parse_osm(self, path):
        try:
            tree = ET.parse(path)
            nodes = {}
            for n in tree.findall(".//node"):
                nodes[n.get("id")] = project_coords(float(n.get("lon")), float(n.get("lat")))
            ways = []
            for w in tree.findall(".//way"):
                if any(t.get("k") == "highway" for t in w.findall("tag")):
                    coords = []
                    for nd in w.findall("nd"):
                        ref = nd.get("ref")
                        if ref in nodes:
                            coords.append(nodes[ref])
                    if len(coords) > 1:
                        ways.append(coords)
            return ways
        except Exception:
            return []

    def load_project_data(self, path):
        try:
            self.project = self.loader.load(path)
            needs_analysis = any(not link.results for link in self.project.network.links.values())
            if needs_analysis:
                self.analysis_service.analyze_project(self.project)
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить проект: {e}")
            self.project = None

    def draw_map(self):
        self.scene.addItem(MapBackgroundItem(self.map_data))

    def draw_network(self):
        if self.project is None:
            return

        self.viz_links = []
        self.link_index = {}
        node_registry = {}

        for node_model in self.project.network.nodes.values():
            if node_model.lon is not None and node_model.lat is not None:
                point = QPointF(*project_coords(node_model.lon, node_model.lat))
            elif node_model.x is not None and node_model.y is not None:
                point = QPointF(node_model.x, node_model.y)
            else:
                continue
            node_label = node_model.name or node_model.id
            node_item = TrafficNode(node_model.id, node_label, point)
            self.scene.addItem(node_item)
            node_registry[node_model.id] = node_item

        for link_model in self.project.network.links.values():
            start_node = node_registry.get(link_model.start_node_id)
            end_node = node_registry.get(link_model.end_node_id)
            if start_node is None or end_node is None:
                continue
            link_item = TrafficLink(link_model, start_node, end_node, self.on_link_click)
            self.scene.addItem(link_item)
            self.viz_links.append(link_item)
            self.link_index[link_model.id] = link_item

        if self.viz_links:
            self.view.fitInView(self.scene.itemsBoundingRect(), Qt.KeepAspectRatio)

    def set_stage(self, s):
        self.current_stage = s
        for link in self.viz_links:
            link.update_visuals(s)
        self.info.clear()

    def on_link_click(self, link_model):
        res = link_model.results
        html = f"<h3>{link_model.name}</h3>"
        html += f"<b>ID:</b> {link_model.id}<br><br>"

        if self.current_stage == 1:
            html += f"<b>Уровень обслуживания (LOS):</b> {res.get('LOS', 'Н/Д')}<br>"
            html += f"<b>Загрузка (V/C):</b> {res.get('VC_ratio', 0)}"
        elif self.current_stage == 2:
            prop = res.get("Optimization_Proposal")
            if prop:
                html += f"<font color='red'><b>Предложение:</b> {prop}</font><br><br>"
                html += f"<b>Ожидаемый V/C:</b> {res.get('VC_optimized', 'Н/Д')}<br>"
                html += f"<b>Ожидаемый LOS:</b> {res.get('LOS_optimized', 'Н/Д')}"
            else:
                html += "<font color='green'>Оптимизация не требуется</font>"
        elif self.current_stage == 3:
            delay = res.get("Delay_sec", 0)
            html += f"<font color='#1976d2' size='4'><b>Доп. задержка:</b> {delay} сек.</font><br>"
            html += "<small>Время, теряемое из-за загрузки участка</small>"
        else:
            html += f"<b>Длина:</b> {link_model.length_km} км<br>"
            html += f"<b>Поток:</b> {link_model.traffic_counts}"

        self.info.setHtml(html)

    def find_route(self):
        if self.project is None or not self.project.network.nodes:
            QMessageBox.warning(self, "Маршрут", "Проект не загружен.")
            return

        node_display = self._get_node_display_pairs()
        if not node_display:
            QMessageBox.warning(self, "Маршрут", "В проекте нет узлов.")
            return

        display_names = [display for display, _ in node_display]
        start_display, ok = QInputDialog.getItem(self, "Маршрут", "Начальный узел:", display_names, 0, False)
        if not ok:
            return
        end_display, ok = QInputDialog.getItem(self, "Маршрут", "Конечный узел:", display_names, 0, False)
        if not ok:
            return
        weight, ok = QInputDialog.getItem(self, "Маршрут", "Критерий:", ["length_km", "travel_time_sec", "delay_sec"], 0, False)
        if not ok:
            return

        display_to_id = {display: node_id for display, node_id in node_display}
        start_node_id = display_to_id[start_display]
        end_node_id = display_to_id[end_display]
        path_link_ids = self.routing_service.find_shortest_path(self.project.network, start_node_id, end_node_id, weight)
        for link in self.viz_links:
            link.is_route_highlighted = link.id in path_link_ids

        self.set_stage(4)
        if not path_link_ids:
            self.info.setHtml("<b>Маршрут не найден.</b>")
            return

        total_length = sum(self.project.network.links[link_id].length_km for link_id in path_link_ids if link_id in self.project.network.links)
        total_delay = sum(self.project.network.links[link_id].results.get("Delay_sec", 0.0) for link_id in path_link_ids if link_id in self.project.network.links)
        self.info.setHtml(
            "<h3>Найден маршрут</h3>"
            f"<b>Связи:</b> {' -> '.join(path_link_ids)}<br>"
            f"<b>Суммарная длина:</b> {round(total_length, 3)} км<br>"
            f"<b>Суммарная задержка:</b> {round(total_delay, 1)} сек"
        )

    def _get_node_display_pairs(self):
        ordered_nodes = sorted(
            self.project.network.nodes.values(),
            key=lambda node: node.name or node.id,
        )
        result = []
        for node in ordered_nodes:
            label = node.name or node.id
            if label != node.id:
                label = f"{label} ({node.id})"
            result.append((label, node.id))
        return result

    def save_current_positions_to_project(self):
        if self.project is None:
            return

        for link in self.viz_links:
            p1 = link.start_node.scenePos()
            p2 = link.end_node.scenePos()
            lon_s, lat_s = unproject_coords(p1.x(), p1.y())
            lon_e, lat_e = unproject_coords(p2.x(), p2.y())
            coords = link.link_model.coords or {}
            if coords.get("type") == "polyline" and len(coords.get("points", [])) >= 2:
                points = coords["points"]
                link.link_model.coords = {
                    "type": "polyline",
                    "points": [
                        [round(lon_s, 6), round(lat_s, 6)],
                        *points[1:-1],
                        [round(lon_e, 6), round(lat_e, 6)],
                    ],
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
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить проект: {e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    sys.exit(app.exec_())
