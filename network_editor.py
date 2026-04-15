import sys
import xml.etree.ElementTree as ET

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent
from PyQt5.QtWidgets import (
    QApplication,
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
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
from project_loader import ProjectLoader
from project_saver import ProjectSaver

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
        self.setPen(QPen(QColor(0, 120, 255), 6, Qt.SolidLine, Qt.RoundCap))
        self.setZValue(50)
        self.update_geometry()

    def update_geometry(self):
        path = QPainterPath()
        path.moveTo(self.start_node.scenePos())
        path.lineTo(self.end_node.scenePos())
        self.setPath(path)


class EditorNode(QGraphicsEllipseItem):
    def __init__(self, node_model: Node, pos, callback):
        radius = 10
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.model = node_model
        self.id = node_model.id
        self.callback = callback
        self._drag_start_pos = None
        self._is_dragging = False
        self._drag_threshold = 5
        self.setPos(pos)
        self.setBrush(QBrush(QColor(255, 80, 80)))
        self.setPen(QPen(Qt.black, 1.5))
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
        self.lbl_status = QLabel("ПКМ: создать узел\nЛКМ: выбрать/соединить\nКолесо: зум")
        panel.addWidget(self.lbl_status)

        btn_save = QPushButton("Сохранить проект")
        btn_save.clicked.connect(self.export_json)
        panel.addWidget(btn_save)

        btn_load = QPushButton("Открыть проект")
        btn_load.clicked.connect(lambda: self.load_project(self.project_file))
        panel.addWidget(btn_load)

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
        if self.scene.items():
            self.view.fitInView(self.scene.itemsBoundingRect(), Qt.KeepAspectRatio)

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

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            view_pos = self.view.mapFrom(self, event.pos())
            scene_pos = self.view.mapToScene(view_pos)

            node_id = f"N{len(self.project.network.nodes)}"
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
                self.links.append(editor_link)
                self.start_node_selection.links.append(editor_link)
                node.links.append(editor_link)
                self.scene.addItem(editor_link)
                self.lbl_status.setText("Дорога создана.")
            else:
                self.lbl_status.setText("Создание дороги отменено.")

        self.start_node_selection.setBrush(QBrush(QColor(255, 80, 80)))
        self.start_node_selection = None

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
        self.project = self.loader.load(project_file)
        self.project_file = project_file
        self.nodes = []
        self.links = []

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
