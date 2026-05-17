import os
import sys
import xml.etree.ElementTree as ET  # <-- ДОБАВИТЬ ЭТУ СТРОКУ
import math

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QBrush, QFont, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent
from PyQt5.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
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

# Цвета для LOS
LOS_COLORS = {
    "A": QColor(0, 200, 0),
    "B": QColor(100, 220, 100),
    "C": QColor(255, 255, 0),
    "D": QColor(255, 165, 0),
    "E": QColor(255, 69, 0),
    "F": QColor(255, 0, 0),
    "UNDEFINED": QColor(200, 200, 200),
}

NODE_COLORS = {
    "boundary": QColor(220, 40, 40),
    "intersection": QColor(45, 90, 210),
    "roundabout_part": QColor(150, 70, 210),
    "ordinary": QColor(80, 80, 80),
}

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

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.app_callback:
            self.app_callback(self.node_model)

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
        coords = link_model.coords or {}
        if coords.get("type") == "polyline":
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
        self.prepareGeometryChange()
        
        # 1. Собираем базовые точки маршрута
        base_points = [self.start_node.scenePos()]
        for pt in self.intermediate_points:
            base_points.append(QPointF(pt[0], pt[1]))
        base_points.append(self.end_node.scenePos())
        
        # 2. Сдвигаем точки вправо по ходу движения
        OFFSET_PX = 4.0 # Сдвиг в пикселях. Можете сделать 5 или 6, если хочется шире
        shifted_points = []
        
        for i in range(len(base_points)):
            # Вычисляем вектор направления линии в этой точке
            if i == 0:
                dx = base_points[1].x() - base_points[0].x()
                dy = base_points[1].y() - base_points[0].y()
            elif i == len(base_points) - 1:
                dx = base_points[-1].x() - base_points[-2].x()
                dy = base_points[-1].y() - base_points[-2].y()
            else:
                # В середине берем усредненный вектор между предыдущей и следующей точкой
                dx = base_points[i+1].x() - base_points[i-1].x()
                dy = base_points[i+1].y() - base_points[i-1].y()
                
            length = math.hypot(dx, dy)
            if length == 0:
                shifted_points.append(base_points[i])
                continue
                
            # Вектор нормали вправо (в координатах экрана Qt ось Y направлена вниз)
            nx = -dy / length
            ny = dx / length
            
            # Применяем смещение
            shifted_x = base_points[i].x() + nx * OFFSET_PX
            shifted_y = base_points[i].y() + ny * OFFSET_PX
            shifted_points.append(QPointF(shifted_x, shifted_y))

        # 3. Строим сам путь по смещенным точкам
        path = QPainterPath()
        path.moveTo(shifted_points[0])
        for pt in shifted_points[1:]:
            path.lineTo(pt)
            
        self.setPath(path)

    def boundingRect(self):
        return super().boundingRect().adjusted(-5, -5, 5, 5)

    def update_visuals(self):
        res = self.link_model.results or {}
        color = LOS_COLORS.get(res.get("LOS", "UNDEFINED"), Qt.gray)
        self.setPen(QPen(color, self.base_width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.app_callback:
            self.app_callback(self.link_model)

    def paint(self, painter, option, widget):
        super().paint(painter, option, widget)
        path = self.path()
        if path.length() < 15:
            return

        percent = 0.9
        point = path.pointAtPercent(percent)
        angle = path.angleAtPercent(percent)

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        
        arrow_color = self.pen().color()
        arrow_color.setAlpha(255) 
        painter.setBrush(QBrush(arrow_color))
        painter.setPen(Qt.NoPen)

        painter.translate(point)
        painter.rotate(-angle)

        arrow_size = self.pen().width() * 1.3
        arrow_head = QPolygonF([
            QPointF(arrow_size, 0),
            QPointF(-arrow_size, -arrow_size * 0.6),
            QPointF(-arrow_size, arrow_size * 0.6)
        ])
        
        painter.drawPolygon(arrow_head)
        painter.restore()

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
        
        if not os.path.exists(data_file) and os.path.exists("osm_network_project_map_nstu.json"):
            data_file = "osm_network_project_map_nstu.json"
            
        self.data_file = data_file
        self.project = None
        self.map_data = self.parse_osm(map_file)
        self.viz_links = []

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

        self.info = QTextEdit()
        self.info.setReadOnly(True)
        control_layout.addWidget(QLabel("Детали объекта:"))
        control_layout.addWidget(self.info)

        self.btn_save_coords = QPushButton("Сохранить координаты в проект")
        self.btn_save_coords.clicked.connect(self.save_current_positions_to_project)
        control_layout.addWidget(self.btn_save_coords)

        self.btn_open_editor = QPushButton("Открыть редактор сети")
        self.btn_open_editor.clicked.connect(self.open_network_editor)
        control_layout.addWidget(self.btn_open_editor)

        self.btn_web_map = QPushButton("Открыть web-карту")
        self.btn_web_map.clicked.connect(self.open_folium_map)
        control_layout.addWidget(self.btn_web_map)

        self.reload_project_and_redraw()

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
            QMessageBox.warning(self, "Ошибка", "Редактор сети недоступен.")
            return
        self.editor_window = NetworkEditor(project_file=self.data_file)
        self.editor_window.show()

    def open_folium_map(self):
        if self.project is None:
            QMessageBox.warning(self, "Web map", "Проект не загружен.")
            return
        if QWebEngineView is None:
            QMessageBox.warning(self, "Web map", f"PyQtWebEngine не импортируется.\nPython: {sys.executable}")
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
            self.web_view.setHtml(build_project_map_html(self.project))
            self.map_stack.setCurrentWidget(self.web_view)
            self.btn_web_map.setText("Вернуться к схеме")
        except Exception as exc:
            QMessageBox.warning(self, "Web map", f"Не удалось построить web-карту:\n{exc}")

    def reload_project_and_redraw(self):
        self.scene.clear()
        self.viz_links = []
        self.draw_map()
        self.load_project_data(self.data_file)
        self.draw_network()
        self.update_all_visuals()

    def parse_osm(self, path):
        try:
            tree = ET.parse(path)
            nodes = {}
            for n in tree.findall(".//node"):
                nodes[n.get("id")] = project_coords(float(n.get("lon")), float(n.get("lat")))
            roads = []
            buildings = []
            allowed_highways = {"primary", "secondary", "tertiary", "residential", "trunk"}
            for w in tree.findall(".//way"):
                tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
                coords = [nodes[nd.get("ref")] for nd in w.findall("nd") if nd.get("ref") in nodes]
                if len(coords) < 2:
                    continue
                if tags.get("highway") in allowed_highways:
                    roads.append({"type": tags["highway"], "coords": coords})
                if tags.get("building") is not None and len(coords) >= 3:
                    buildings.append({"type": tags["building"], "coords": coords})
            return {"roads": roads, "buildings": buildings}
        except Exception as exc:
            print(f"OSM parse error: {exc}")
            return {"roads": [], "buildings": []}

    def load_project_data(self, path):
        try:
            self.project = self.loader.load(path)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить проект: {exc}")
            self.project = None

    def draw_map(self):
        self.scene.addItem(MapBackgroundItem(self.map_data))

    def draw_network(self):
        if self.project is None:
            return
        node_registry = {}
        for node_model in self.project.network.nodes.values():
            if node_model.lon is not None and node_model.lat is not None:
                point = QPointF(*project_coords(node_model.lon, node_model.lat))
            elif node_model.x is not None and node_model.y is not None:
                point = QPointF(node_model.x, node_model.y)
            else:
                continue
            node_label = node_model.name or node_model.id
            node_item = TrafficNode(node_model, node_label, point, self.on_node_click)
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
            
        if self.viz_links:
            self.view.fitInView(self.scene.itemsBoundingRect(), Qt.KeepAspectRatio)

    def update_all_visuals(self):
        for link in self.viz_links:
            link.update_visuals()

    def on_node_click(self, node_model):
        incoming = self.project.network.get_incoming_links(node_model.id)
        outgoing = self.project.network.get_outgoing_links(node_model.id)
        html = f"<h3>{node_model.name or node_model.id}</h3>"
        html += f"<b>ID:</b> {node_model.id}<br>"
        html += f"<b>lon/lat:</b> {node_model.lon}, {node_model.lat}<br>"
        html += f"<b>Входящих дорог:</b> {len(incoming)}<br>"
        html += f"<b>Исходящих дорог:</b> {len(outgoing)}<br>"
        self.info.setHtml(html)

    def on_link_click(self, link_model):
        res = link_model.results or {}
        html = f"<h3>{link_model.name}</h3>"
        html += f"<b>ID:</b> {link_model.id}<br>"
        html += f"<b>От:</b> {link_model.start_node_id}<br>"
        html += f"<b>До:</b> {link_model.end_node_id}<br>"
        html += f"<b>Длина:</b> {link_model.length_km} км<br>"
        html += f"<b>Класс OSM:</b> {link_model.metadata.get('highway', 'Н/Д')}<br>"
        html += f"<b>Полос:</b> {link_model.parameters.get('lanes_total', 1)}<br><br>"
        
        html += f"<b>==== Результаты CTM ====</b><br>"
        html += f"<b>LOS:</b> {res.get('LOS', 'Н/Д')}<br>"
        html += f"<b>Загрузка (V/C):</b> {res.get('VC_ratio', 'Н/Д')}<br>"
        html += f"<b>Плотность:</b> {res.get('Density_pcu_km', 'Н/Д')} авт/км<br>"

        self.info.setHtml(html)

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
                    "points": [[round(lon_s, 6), round(lat_s, 6)], *points[1:-1], [round(lon_e, 6), round(lat_e, 6)]],
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
    window.resize(1200, 800)
    window.show()
    sys.exit(app.exec_())