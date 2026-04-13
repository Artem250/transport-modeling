import sys
import json
import math
import xml.etree.ElementTree as ET
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QGraphicsView, QGraphicsScene,
    QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QMessageBox,
    QInputDialog, QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsItem,
    QLabel
)
from PyQt5.QtGui import QPen, QBrush, QColor, QPainter, QPolygonF, QPainterPath, QWheelEvent
from PyQt5.QtCore import Qt, QPointF, QRectF

# --- КОНФИГУРАЦИЯ ПРОЕКЦИИ ---
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


# --- ГРАФИЧЕСКИЕ ЭЛЕМЕНТЫ ---

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
    def __init__(self, link_id, start_node, end_node):
        super().__init__()
        self.id = link_id
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
    def __init__(self, node_id, pos, callback):
        radius = 10
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.id = node_id
        self.callback = callback

        # --- Новые переменные для фильтрации клика и перетаскивания ---
        self._drag_start_pos = None
        self._is_dragging = False
        self._drag_threshold = 5  # пикселей
        # -----------------------------------------------------------

        self.setPos(pos)
        self.setBrush(QBrush(QColor(255, 80, 80)))
        self.setPen(QPen(Qt.black, 1.5))
        self.setZValue(100)

        # Флаги оставляем те же
        self.setFlags(QGraphicsItem.ItemIsMovable |
                      QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsGeometryChanges)
        self.links = []

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            for link in self.links:
                link.update_geometry()
        return super().itemChange(change, value)

    def mousePressEvent(self, event):
        """Запоминаем позицию в момент нажатия"""
        if event.button() == Qt.LeftButton:
            self._drag_start_pos = event.screenPos()
            self._is_dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Проверяем, не сдвинулся ли узел слишком сильно"""
        if self._drag_start_pos is not None:
            # Считаем расстояние от точки нажатия до текущей точки курсора
            dist = (event.screenPos() - self._drag_start_pos).manhattanLength()
            if dist > self._drag_threshold:
                self._is_dragging = True
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Вызываем callback (соединение) ТОЛЬКО если это был чистый клик без перетаскивания"""
        if event.button() == Qt.LeftButton:
            # Если мы НЕ тащили узел — вызываем логику соединения
            if not self._is_dragging:
                self.callback(self)

            # Сброс состояния
            self._drag_start_pos = None
            self._is_dragging = False

        super().mouseReleaseEvent(event)


# --- УЛУЧШЕННЫЙ ВИД С ЗУМОМ ---

class EditorView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)  # Перемещение карты зажатием ЛКМ (если не на узле)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)  # Зум в точку курсора

    def wheelEvent(self, event: QWheelEvent):
        factor = 1.15
        if event.angleDelta().y() > 0:
            self.scale(factor, factor)
        else:
            self.scale(1 / factor, 1 / factor)


# --- ГЛАВНОЕ ОКНО РЕДАКТОРА ---

class NetworkEditor(QMainWindow):
    def __init__(self, map_osm="map.osm"):
        super().__init__()
        self.setWindowTitle("Редактор сети (Колесо мыши для Зума)")
        self.resize(1200, 800)

        self.nodes = []
        self.links = []
        self.start_node_selection = None

        self.map_data = self.parse_osm(map_osm)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        self.scene = QGraphicsScene()
        # Используем наш кастомный EditorView вместо обычного QGraphicsView
        self.view = EditorView(self.scene)
        layout.addWidget(self.view, 4)

        panel = QVBoxLayout()
        self.lbl_status = QLabel("ПКМ: Создать узел\nЛКМ: Выбрать/Соединить\nКолесо: Зум")
        panel.addWidget(self.lbl_status)

        btn_save = QPushButton("Экспорт в JSON")
        btn_save.clicked.connect(self.export_json)
        panel.addWidget(btn_save)

        panel.addStretch()
        layout.addLayout(panel, 1)

        self.scene.addItem(MapBackgroundItem(self.map_data))
        if self.map_data:
            self.view.fitInView(self.scene.itemsBoundingRect(), Qt.KeepAspectRatio)

    def parse_osm(self, path):
        try:
            tree = ET.parse(path)
            nodes = {}
            for n in tree.findall(".//node"):
                nodes[n.get('id')] = project_coords(float(n.get('lon')), float(n.get('lat')))
            ways = []
            for w in tree.findall(".//way"):
                if any(t.get('k') == 'highway' for t in w.findall("tag")):
                    coords = []
                    for nd in w.findall("nd"):
                        ref = nd.get('ref')
                        if ref in nodes: coords.append(nodes[ref])
                    if len(coords) > 1: ways.append(coords)
            return ways
        except:
            return []

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            # Получаем позицию на сцене через view
            view_pos = self.view.mapFrom(self, event.pos())
            scene_pos = self.view.mapToScene(view_pos)

            node_id = f"N{len(self.nodes)}"
            node = EditorNode(node_id, scene_pos, self.handle_node_click)
            self.scene.addItem(node)
            self.nodes.append(node)
        super().mousePressEvent(event)

    def handle_node_click(self, node):
        if self.start_node_selection is None:
            self.start_node_selection = node
            node.setBrush(QBrush(Qt.yellow))
            self.lbl_status.setText(f"Выбрано: {node.id}. Выберите второй узел.")
        else:
            if node == self.start_node_selection:
                node.setBrush(QBrush(QColor(255, 80, 80)))
                self.start_node_selection = None
                self.lbl_status.setText("Сброшено.")
            else:
                link_id, ok = QInputDialog.getText(self, "Создать связь", "ID дороги:")
                if ok and link_id:
                    new_link = EditorLink(link_id, self.start_node_selection, node)
                    self.scene.addItem(new_link)
                    self.links.append(new_link)
                    self.start_node_selection.links.append(new_link)
                    node.links.append(new_link)

                self.start_node_selection.setBrush(QBrush(QColor(255, 80, 80)))
                self.start_node_selection = None
                self.lbl_status.setText("Дорога создана.")

    def export_json(self):
        data = {
            "project_name": "Editor Export",
            "pcu_coefficients": {"car": 1.0, "truck": 2.5, "bus": 2.0},
            "directional_links": [],
            "routes": []
        }
        for link in self.links:
            p1 = link.start_node.scenePos()
            p2 = link.end_node.scenePos()
            lon1, lat1 = unproject_coords(p1.x(), p1.y())
            lon2, lat2 = unproject_coords(p2.x(), p2.y())

            link_entry = {
                "id": link.id, "name": f"Road {link.id}", "type": "straight",
                "length_km": 0.5, "traffic_counts": {"car": 500},
                "coords": {
                    "lon_start": round(lon1, 6), "lat_start": round(lat1, 6),
                    "lon_end": round(lon2, 6), "lat_end": round(lat2, 6)
                }
            }
            data["directional_links"].append(link_entry)

        with open("manual_network.json", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        QMessageBox.information(self, "Save", "Файл manual_network.json создан!")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    editor = NetworkEditor("map.osm")
    editor.show()
    sys.exit(app.exec_())