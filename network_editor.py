import sys
import xml.etree.ElementTree as ET

from PyQt5.QtCore import QPointF, QRectF, QTimer, Qt
from PyQt5.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent
from PyQt5.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from models import Link, Network, Node, Project
from network_migration import ensure_dynamic_schema
from osm_project_importer import OsmImportError, build_project_from_osm_point, build_project_from_osm_xml
from project_loader import ProjectLoader
from project_saver import ProjectSaver

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except ImportError:
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
    return lon * 100000, -lat * 100000


def unproject_coords(x, y_qt):
    if USE_PYPROJ:
        lon, lat = inv_transformer.transform(x, -y_qt)
        return lon, lat
    return x / 100000, -y_qt / 100000


class MapBackgroundItem(QGraphicsItem):
    def __init__(self, ways_data):
        super().__init__()
        self.ways = ways_data
        self.pen = QPen(QColor(180, 180, 180), 1)
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


class EditorLink(QGraphicsPathItem):
    def __init__(self, link_model: Link, start_node, end_node):
        super().__init__()
        self.model = link_model
        self.id = link_model.id
        self.start_node = start_node
        self.end_node = end_node
        self.intermediate_points = []
        coords = link_model.coords or {}
        if coords.get("type") == "polyline":
            for lon, lat in coords.get("points", [])[1:-1]:
                self.intermediate_points.append(project_coords(lon, lat))
        self.setPen(QPen(QColor(0, 120, 255), 6, Qt.SolidLine, Qt.RoundCap))
        self.setZValue(50)
        self.update_geometry()

    def update_geometry(self):
        path = QPainterPath()
        path.moveTo(self.start_node.scenePos())
        for x, y in self.intermediate_points:
            path.lineTo(QPointF(x, y))
        path.lineTo(self.end_node.scenePos())
        self.setPath(path)


class EditorNode(QGraphicsEllipseItem):
    def __init__(self, node_model: Node, pos, callback):
        radius = 5
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.model = node_model
        self.id = node_model.id
        self.label = node_model.name or node_model.id
        self.callback = callback
        self._drag_start_pos = None
        self._is_dragging = False
        self._drag_threshold = 5
        self.setPos(pos)
        self.setBrush(QBrush(QColor(255, 80, 80)))
        self.setPen(QPen(Qt.black, 1))
        self.setZValue(100)
        self.setFlags(QGraphicsItem.ItemIsMovable | QGraphicsItem.ItemIsSelectable | QGraphicsItem.ItemSendsGeometryChanges)
        self.links = []

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            for link in self.links:
                link.update_geometry()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.screenPos()
            self._is_dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is not None:
            dist = (event.screenPos() - self._drag_start_pos).manhattanLength()
            if dist > self._drag_threshold:
                self._is_dragging = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if not self._is_dragging:
                self.callback(self)
            self._drag_start_pos = None
            self._is_dragging = False
        super().mouseReleaseEvent(event)

    def paint(self, painter, option, widget):
        super().paint(painter, option, widget)
        font = QFont("Arial", 3)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(Qt.white)
        painter.drawText(self.rect(), Qt.AlignCenter, self.label)


class EditorView(QGraphicsView):
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


class NetworkEditor(QMainWindow):
    def __init__(self, map_osm="map.osm", project_file="manual_network.json"):
        super().__init__()
        self.setWindowTitle("Редактор сети")
        self.resize(1200, 800)

        self.loader = ProjectLoader()
        self.saver = ProjectSaver()
        self.project_file = project_file

        self.project = Project(
            project_name="Editor Export",
            pcu_coefficients={"car": 1.0, "truck": 2.5, "bus": 2.0},
            network=Network(),
            metadata={"source": "network_editor"},
        )
        ensure_dynamic_schema(self.project)

        self.nodes = []
        self.links = []
        self.start_node_selection = None
        self.map_data = self.parse_osm(map_osm)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        self.scene = QGraphicsScene()
        self.view = EditorView(self.scene)
        layout.addWidget(self.view, 4)

        panel = QVBoxLayout()
        self.lbl_status = QLabel("ПКМ: создать узел\nЛКМ: выбрать/соединить\nDel: удалить выбранный узел\nКолесо: зум")
        panel.addWidget(self.lbl_status)

        btn_save = QPushButton("Сохранить проект")
        btn_save.clicked.connect(self.export_json)
        panel.addWidget(btn_save)

        btn_load = QPushButton("Открыть проект")
        btn_load.clicked.connect(lambda: self.load_project(self.project_file))
        panel.addWidget(btn_load)

        btn_import_osm = QPushButton("Загрузить участок OSM")
        btn_import_osm.clicked.connect(self.import_osm_area)
        panel.addWidget(btn_import_osm)

        btn_import_osm_file = QPushButton("Импорт из OSM-файла")
        btn_import_osm_file.clicked.connect(self.import_osm_file)
        panel.addWidget(btn_import_osm_file)

        btn_web_map = QPushButton("Открыть web-карту")
        btn_web_map.clicked.connect(self.open_folium_map)
        panel.addWidget(btn_web_map)

        btn_delete = QPushButton("Удалить выбранный узел")
        btn_delete.clicked.connect(self.delete_selected_nodes)
        panel.addWidget(btn_delete)

        panel.addStretch()
        layout.addLayout(panel, 1)

        self.redraw_scene()
        try:
            self.load_project(self.project_file)
        except Exception:
            pass

    def redraw_scene(self):
        self.scene.clear()
        self.scene.addItem(MapBackgroundItem(self.map_data))
        for node in self.nodes:
            self.scene.addItem(node)
        for link in self.links:
            self.scene.addItem(link)
        self.fit_scene_to_network()

    def fit_scene_to_network(self):
        items = self.links or self.nodes or self.scene.items()
        if not items:
            return
        rect = QRectF()
        for item in items:
            rect = rect.united(item.sceneBoundingRect()) if rect.isValid() else item.sceneBoundingRect()
        if not rect.isValid() or rect.isEmpty():
            return
        margin = max(rect.width(), rect.height(), 100.0) * 0.08
        rect = rect.adjusted(-margin, -margin, margin, margin)
        self.scene.setSceneRect(rect)
        self.view.resetTransform()
        self.view.fitInView(rect, Qt.KeepAspectRatio)
        self.view.centerOn(rect.center())

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

    def project_background_ways(self, project):
        ways = []
        for link in project.network.links.values():
            coords = link.coords or {}
            if coords.get("type") == "polyline":
                points = [project_coords(lon, lat) for lon, lat in coords.get("points", [])]
            else:
                lon_start = coords.get("lon_start")
                lat_start = coords.get("lat_start")
                lon_end = coords.get("lon_end")
                lat_end = coords.get("lat_end")
                if None in (lon_start, lat_start, lon_end, lat_end):
                    continue
                points = [project_coords(lon_start, lat_start), project_coords(lon_end, lat_end)]
            if len(points) > 1:
                ways.append(points)
        return ways

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            view_pos = self.view.mapFrom(self, event.pos())
            scene_pos = self.view.mapToScene(view_pos)

            node_id = self._next_node_id()
            lon, lat = unproject_coords(scene_pos.x(), scene_pos.y())
            node_model = Node(
                id=node_id,
                name=node_id,
                lon=round(lon, 6),
                lat=round(lat, 6),
                x=round(scene_pos.x(), 3),
                y=round(scene_pos.y(), 3),
            )
            editor_node = EditorNode(node_model, scene_pos, self.handle_node_click)
            self.project.network.add_node(node_model)
            self.nodes.append(editor_node)
            self.scene.addItem(editor_node)
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Delete:
            self.delete_selected_nodes()
            return
        super().keyPressEvent(event)

    def _next_node_id(self):
        existing = set(self.project.network.nodes.keys())
        idx = 1
        while f"N{idx}" in existing:
            idx += 1
        return f"N{idx}"

    def handle_node_click(self, node):
        if self.start_node_selection is None:
            self.start_node_selection = node
            node.setBrush(QBrush(Qt.yellow))
            self.lbl_status.setText(f"Выбрано: {node.id}. Выберите второй узел.")
            return

        if node == self.start_node_selection:
            node.setBrush(QBrush(QColor(255, 80, 80)))
            self.start_node_selection = None
            self.lbl_status.setText("Сброшено.")
            return

        link_id, ok = QInputDialog.getText(self, "Создать связь", "ID дороги:")
        if ok and link_id:
            link_model = self.collect_link_model(link_id, self.start_node_selection, node)
            if link_model is not None:
                editor_link = EditorLink(link_model, self.start_node_selection, node)
                self.project.network.add_link(link_model)
                ensure_dynamic_schema(self.project)
                self.links.append(editor_link)
                self.start_node_selection.links.append(editor_link)
                node.links.append(editor_link)
                self.scene.addItem(editor_link)
                self.lbl_status.setText("Дорога создана.")
            else:
                self.lbl_status.setText("Создание дороги отменено.")

        self.start_node_selection.setBrush(QBrush(QColor(255, 80, 80)))
        self.start_node_selection = None

    def remove_link(self, editor_link):
        if editor_link in self.links:
            self.links.remove(editor_link)
        if editor_link in editor_link.start_node.links:
            editor_link.start_node.links.remove(editor_link)
        if editor_link in editor_link.end_node.links:
            editor_link.end_node.links.remove(editor_link)
        if editor_link.id in self.project.network.links:
            del self.project.network.links[editor_link.id]
        self.project.network.sources = {
            source_id: source
            for source_id, source in self.project.network.sources.items()
            if source.link_id != editor_link.id
        }
        self.project.network.sinks = {
            sink_id: sink
            for sink_id, sink in self.project.network.sinks.items()
            if sink.link_id != editor_link.id
        }
        self.project.network.movements = {
            movement_id: movement
            for movement_id, movement in self.project.network.movements.items()
            if movement.from_link_id != editor_link.id and movement.to_link_id != editor_link.id
        }
        ensure_dynamic_schema(self.project)
        for route in self.project.network.routes.values():
            route.link_ids = [link_id for link_id in route.link_ids if link_id != editor_link.id]
        if editor_link.scene() is not None:
            self.scene.removeItem(editor_link)

    def remove_node(self, editor_node):
        related_links = list(editor_node.links)
        for editor_link in related_links:
            self.remove_link(editor_link)

        if self.start_node_selection is editor_node:
            self.start_node_selection = None
            self.lbl_status.setText("Выбор сброшен.")

        if editor_node in self.nodes:
            self.nodes.remove(editor_node)
        if editor_node.id in self.project.network.nodes:
            del self.project.network.nodes[editor_node.id]
        if editor_node.scene() is not None:
            self.scene.removeItem(editor_node)

    def delete_selected_nodes(self):
        selected_nodes = [item for item in self.scene.selectedItems() if isinstance(item, EditorNode)]
        if not selected_nodes:
            QMessageBox.information(self, "Удаление", "Выберите узел для удаления.")
            return

        unique_links = {link.id for node in selected_nodes for link in node.links}
        reply = QMessageBox.question(
            self,
            "Удаление узлов",
            f"Удалить узлов: {len(selected_nodes)}.\nСвязанных дорог будет удалено: {len(unique_links)}.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        for node in list(selected_nodes):
            self.remove_node(node)

        self.lbl_status.setText("Узел и связанные дороги удалены.")

    def collect_link_model(self, link_id, start_node, end_node):
        name, ok = QInputDialog.getText(self, "Параметры связи", "Название дороги:", text=f"Road {link_id}")
        if not ok:
            return None

        link_type, ok = QInputDialog.getItem(self, "Параметры связи", "Тип участка:", ["straight", "intersection"], 0, False)
        if not ok:
            return None

        length_km, ok = QInputDialog.getDouble(self, "Параметры связи", "Длина, км:", 0.5, 0.01, 100.0, 3)
        if not ok:
            return None

        cars, ok = QInputDialog.getInt(self, "Параметры связи", "Интенсивность автомобилей, ед/ч:", 500, 0, 100000, 10)
        if not ok:
            return None

        if link_type == "straight":
            lanes_total, ok = QInputDialog.getInt(self, "Параметры связи", "Число полос:", 1, 1, 20, 1)
            if not ok:
                return None
            parameters = {
                "length_km": length_km,
                "lanes_total": lanes_total,
                "lanes_bus": 0,
                "capacity_per_lane_base": 1800,
                "lane_width_m": 3.5,
                "grade_percent": 0.0,
                "parking_present": False,
                "heavy_vehicles_percent": 0.0,
            }
        else:
            cycle_time, ok = QInputDialog.getInt(self, "Параметры перекрестка", "Длительность цикла, сек:", 100, 10, 300, 5)
            if not ok:
                return None
            green_time, ok = QInputDialog.getInt(self, "Параметры перекрестка", "Разрешающий сигнал, сек:", 30, 5, 300, 5)
            if not ok:
                return None
            parameters = {
                "length_km": length_km,
                "cycle_time": cycle_time,
                "green_time": green_time,
                "saturation_flow_base": 1800,
                "lanes_count": 1,
                "lane_width_m": 3.5,
                "grade_percent": 0.0,
                "parking_present": False,
                "heavy_vehicles_percent": 0.0,
                "g_others": max(cycle_time - green_time, 0),
            }

        p1 = start_node.scenePos()
        p2 = end_node.scenePos()
        lon1, lat1 = unproject_coords(p1.x(), p1.y())
        lon2, lat2 = unproject_coords(p2.x(), p2.y())

        return Link(
            id=link_id,
            name=name,
            start_node_id=start_node.id,
            end_node_id=end_node.id,
            link_type=link_type,
            length_km=length_km,
            traffic_counts={"car": cars},
            parameters=parameters,
            coords={
                "lon_start": round(lon1, 6),
                "lat_start": round(lat1, 6),
                "lon_end": round(lon2, 6),
                "lat_end": round(lat2, 6),
            },
            metadata={},
        )

    def load_project(self, project_file):
        project = self.loader.load(project_file)
        self.apply_project(project)
        self.project_file = project_file

    def apply_project(self, project):
        self.project = project
        ensure_dynamic_schema(self.project)
        self.nodes = []
        self.links = []
        self.start_node_selection = None

        node_map = {}
        for node_model in self.project.network.nodes.values():
            if node_model.x is not None and node_model.y is not None:
                pos = QPointF(node_model.x, node_model.y)
            elif node_model.lon is not None and node_model.lat is not None:
                pos = QPointF(*project_coords(node_model.lon, node_model.lat))
            else:
                continue
            editor_node = EditorNode(node_model, pos, self.handle_node_click)
            self.nodes.append(editor_node)
            node_map[node_model.id] = editor_node

        for link_model in self.project.network.links.values():
            start_node = node_map.get(link_model.start_node_id)
            end_node = node_map.get(link_model.end_node_id)
            if start_node is None or end_node is None:
                continue
            editor_link = EditorLink(link_model, start_node, end_node)
            self.links.append(editor_link)
            start_node.links.append(editor_link)
            end_node.links.append(editor_link)

        self.redraw_scene()

    def import_osm_area(self):
        lat, ok = QInputDialog.getDouble(self, "OSM", "Широта центра:", 54.841, -90.0, 90.0, 6)
        if not ok:
            return
        lon, ok = QInputDialog.getDouble(self, "OSM", "Долгота центра:", 83.106, -180.0, 180.0, 6)
        if not ok:
            return
        dist_m, ok = QInputDialog.getInt(self, "OSM", "Радиус загрузки, м:", 1500, 100, 10000, 100)
        if not ok:
            return
        intensity, ok = QInputDialog.getInt(self, "OSM", "Базовая интенсивность, авто/ч:", 600, 0, 100000, 50)
        if not ok:
            return

        try:
            project = build_project_from_osm_point((lat, lon), dist_m, intensity)
        except OsmImportError as e:
            QMessageBox.warning(
                self,
                "OSM",
                str(e),
            )
            return
        except Exception as e:
            QMessageBox.critical(self, "OSM", f"Не удалось загрузить участок карты:\n{e}")
            return

        self.map_data = self.project_background_ways(project)
        self.apply_project(project)
        QTimer.singleShot(0, self.fit_scene_to_network)
        self.project_file = "osm_network_project.json"
        self.lbl_status.setText(
            f"Загружено из OSM: узлов {len(project.network.nodes)}, дорог {len(project.network.links)}. "
            "Нажмите сохранение проекта, чтобы записать osm_network_project.json."
        )

    def import_osm_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "OSM", "", "OSM files (*.osm *.xml);;All files (*)")
        if not file_path:
            return
        intensity, ok = QInputDialog.getInt(self, "OSM", "Базовая интенсивность, авто/ч:", 600, 0, 100000, 50)
        if not ok:
            return

        try:
            project = build_project_from_osm_xml(file_path, intensity)
        except Exception as e:
            QMessageBox.critical(self, "OSM", f"Не удалось импортировать OSM-файл:\n{e}")
            return

        self.map_data = self.project_background_ways(project)
        self.apply_project(project)
        QTimer.singleShot(0, self.fit_scene_to_network)
        self.project_file = "osm_network_project.json"
        self.lbl_status.setText(
            f"Импортировано из файла:\n узлов {len(project.network.nodes)},\n дорог {len(project.network.links)}. "
            "\nНажмите сохранение проекта,\n чтобы записать osm_network_project.json."
        )

    def open_folium_map(self):
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
        try:
            from folium_map_viewer import FoliumMapWindow
        except Exception as e:
            QMessageBox.warning(
                self,
                "Web map",
                f"Не удалось открыть web-карту:\n{e}",
            )
            return

        try:
            self.folium_window = FoliumMapWindow(self.project, "Traffic map")
            self.folium_window.show()
        except Exception as e:
            QMessageBox.warning(self, "Web map", f"Не удалось построить web-карту:\n{e}")

    def export_json(self):
        for editor_node in self.nodes:
            pos = editor_node.scenePos()
            lon, lat = unproject_coords(pos.x(), pos.y())
            editor_node.model.x = round(pos.x(), 3)
            editor_node.model.y = round(pos.y(), 3)
            editor_node.model.lon = round(lon, 6)
            editor_node.model.lat = round(lat, 6)

        for editor_link in self.links:
            p1 = editor_link.start_node.scenePos()
            p2 = editor_link.end_node.scenePos()
            lon1, lat1 = unproject_coords(p1.x(), p1.y())
            lon2, lat2 = unproject_coords(p2.x(), p2.y())
            editor_link.model.start_node_id = editor_link.start_node.id
            editor_link.model.end_node_id = editor_link.end_node.id
            coords = editor_link.model.coords or {}
            if coords.get("type") == "polyline" and len(coords.get("points", [])) >= 2:
                points = coords["points"]
                editor_link.model.coords = {
                    "type": "polyline",
                    "points": [
                        [round(lon1, 6), round(lat1, 6)],
                        *points[1:-1],
                        [round(lon2, 6), round(lat2, 6)],
                    ],
                    "lon_start": round(lon1, 6),
                    "lat_start": round(lat1, 6),
                    "lon_end": round(lon2, 6),
                    "lat_end": round(lat2, 6),
                }
            else:
                editor_link.model.coords = {
                    "lon_start": round(lon1, 6),
                    "lat_start": round(lat1, 6),
                    "lon_end": round(lon2, 6),
                    "lat_end": round(lat2, 6),
                }

        self.saver.save(self.project, self.project_file)
        QMessageBox.information(self, "Save", f"Файл {self.project_file} сохранен.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    editor = NetworkEditor("map.osm")
    editor.show()
    sys.exit(app.exec_())
