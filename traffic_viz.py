import sys
import json
import math
import xml.etree.ElementTree as ET
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QGraphicsView, QGraphicsScene,
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QRadioButton,
    QGroupBox, QTextEdit, QGraphicsItem, QGraphicsLineItem,
    QGraphicsEllipseItem, QGraphicsPathItem, QPushButton, QMessageBox
)
from PyQt5.QtGui import (
    QPen, QBrush, QColor, QPainter, QWheelEvent, QPolygonF, QPainterPath
)
from PyQt5.QtCore import Qt, QPointF, QRectF, QLineF

# --- КОНФИГУРАЦИЯ ПРОЕКЦИИ ---
try:
    from pyproj import Transformer

    # WGS84 -> UTM Zone 44N (Новосибирск)
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:32644", always_xy=True)
    # Обратный трансформер для сохранения координат
    inv_transformer = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)
    USE_PYPROJ = True
except ImportError:
    USE_PYPROJ = False


def project_coords(lon, lat):
    """Переводит (lon, lat) -> (x, y в метрах)."""
    if USE_PYPROJ:
        x, y = transformer.transform(lon, lat)
        return x, -y  # Инвертируем Y для Qt
    else:
        # Упрощенная проекция Меркатора
        r_major = 6378137.0
        x = r_major * math.radians(lon)
        scale = x / lon if lon != 0 else 1
        y = 180.0 / math.pi * math.log(math.tan(math.pi / 4.0 + lat * (math.pi / 180.0) / 2.0)) * scale
        return x, -y


def unproject_coords(x, y_qt):
    """Переводит (x, y в метрах) -> (lon, lat)."""
    if USE_PYPROJ:
        # Инвертируем Y обратно перед трансформацией
        lon, lat = inv_transformer.transform(x, -y_qt)
        return lon, lat
    else:
        # Очень грубая обратная проекция для случая без pyproj
        r_major = 6378137.0
        lon = math.degrees(x / r_major)
        lat = math.degrees(2 * math.atan(math.exp(math.radians(-y_qt))) - math.pi / 2)
        return lon, lat


# Цвета для LOS
LOS_COLORS = {
    'A': QColor(0, 200, 0),  # Зеленый
    'B': QColor(100, 220, 100),
    'C': QColor(255, 255, 0),  # Желтый
    'D': QColor(255, 165, 0),  # Оранжевый
    'E': QColor(255, 69, 0),  # Красно-оранжевый
    'F': QColor(255, 0, 0),  # Красный
    'UNDEFINED': QColor(200, 200, 200)
}


# --- ГРАФИЧЕСКИЕ ОБЪЕКТЫ ---

class MapBackgroundItem(QGraphicsItem):
    """Фон карты из map.txt"""

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
    """Графическое представление узла (перекрестка)."""

    def __init__(self, node_id, pos_point):
        radius = 8
        # Рисуем круг с центром в (0,0) относительно позиции айтема
        super().__init__(-radius, -radius, radius * 2, radius * 2)
        self.node_id = node_id
        self.setPos(pos_point)
        self.setBrush(QBrush(QColor(50, 50, 150)))
        self.setPen(QPen(Qt.black, 2))
        self.setZValue(100)  # Узлы всегда сверху

        # Делаем узел перемещаемым
        self.setFlags(QGraphicsItem.ItemIsMovable |
                      QGraphicsItem.ItemIsSelectable |
                      QGraphicsItem.ItemSendsScenePositionChanges)

        self.connected_links = []  # Список ссылок на TrafficLink

    def add_link(self, link):
        if link not in self.connected_links:
            self.connected_links.append(link)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange:
            # Когда узел перемещается, просим все связанные дороги обновить геометрию
            for link in self.connected_links:
                link.update_geometry()
        return super().itemChange(change, value)


class TrafficLink(QGraphicsPathItem):
    """Графическое представление дороги."""

    def __init__(self, link_data, start_node, end_node, app_callback=None):
        super().__init__()
        self.data = link_data
        self.id = link_data['id']
        self.start_node = start_node
        self.end_node = end_node
        self.app_callback = app_callback

        # Проверяем, является ли это кольцом (из параметров или по ID)
        res = link_data.get('results', {})
        self.is_ring = res.get('is_ring', False) or ('RING' in self.id and 'CIRCULATION' in self.id)

        # Обработка промежуточных точек для обычных кривых дорог
        self.intermediate_points = []
        coords = link_data.get('coords', {})
        if not self.is_ring and coords.get('type') == 'polyline':
            raw_points = coords.get('points', [])
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
        """Пересчитывает форму линии на основе позиций узлов."""
        path = QPainterPath()
        p1 = self.start_node.scenePos()
        p2 = self.end_node.scenePos()

        if self.is_ring:
            # 1. Вектор между узлами
            vec = p2 - p1
            dist = math.sqrt(vec.x() ** 2 + vec.y() ** 2)

            if dist > 1.0:
                # 2. Математика для угла 90 градусов
                # Радиус круга, чтобы хорда dist стягивала 90 градусов
                radius = dist / math.sqrt(2)

                # Находим центр круга.
                # Он лежит на перпендикуляре к середине отрезка p1-p2.
                mid = (p1 + p2) / 2

                # Перпендикулярный вектор (повернут на 90 градусов)
                # Чтобы изменить сторону "выпуклости", поменяйте знаки у perp_x/y
                perp_x = -(p2.y() - p1.y()) / dist
                perp_y = (p2.x() - p1.x()) / dist

                # Расстояние от середины хорды до центра круга (h)
                # Для 90 градусов h = L / 2
                h = dist / 2

                center_x = mid.x() + perp_x * h
                center_y = mid.y() + perp_y * h

                # 3. Рисуем круг через центр и радиус
                # Важно: QRectF(x, y, w, h) принимает ТОЧКУ УГЛА, а не центра
                rect = QRectF(center_x - radius, center_y - radius, radius * 2, radius * 2)
                path.addEllipse(rect)
            else:
                path.moveTo(p1)
                path.lineTo(p2)
        else:
            # Обычная прямая
            path.moveTo(p1)
            for pt in self.intermediate_points:
                path.lineTo(QPointF(pt[0], pt[1]))
            path.lineTo(p2)

        self.setPath(path)

    def update_visuals(self, stage):
        """Обновляет цвет и толщину в зависимости от выбранного режима визуализации."""
        res = self.data.get('results', {})
        color = Qt.gray
        width = self.base_width

        if stage == 1:  # Режим LOS
            los = res.get('LOS', 'F')
            color = LOS_COLORS.get(los, Qt.gray)

        elif stage == 2:  # Режим Оптимизации
            prop = res.get('Optimization_Proposal')
            if prop:
                color = QColor(255, 0, 0)
                width = 12
            else:
                color = QColor(0, 200, 0, 80)
        elif stage == 3:  # Маршруты
            d = res.get('Delay_sec', 0)
            if d > 60:
                color = QColor(200, 0, 0)
            elif d > 10:
                color = QColor(200, 200, 0)
            else:
                color = QColor(200, 200, 200)

        pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        self.setPen(pen)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if self.app_callback:
            self.app_callback(self.data)


# --- ОСНОВНОЕ ОКНО ---

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
    def __init__(self, map_file="map.osm", data_file="viz_data.json"):
        super().__init__()
        self.setWindowTitle("Транспортный Визуализатор")
        self.resize(1400, 900)

        self.map_data = self.parse_osm(map_file)
        self.traffic_data = self.load_data(data_file)
        self.data_file = data_file
        # Основной интерфейс
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QHBoxLayout(central_widget)

        # Сцена и Вид
        self.scene = QGraphicsScene()
        self.view = MapViewer(self.scene)
        # self.view = QGraphicsView(self.scene)
        # self.view.setRenderHint(QPainter.Antialiasing)
        # self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        layout.addWidget(self.view, 4)

        # Правая панель управления
        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)
        layout.addWidget(control_panel, 1)

        # Группа переключения слоев
        group_box = QGroupBox("Режим визуализации")
        group_layout = QVBoxLayout()

        self.rb1 = QRadioButton("1. V/C и LOS")
        self.rb1.setChecked(True)
        self.rb2 = QRadioButton("2. Оптимизация")
        self.rb3 = QRadioButton("3. Задержки")

        self.rb1.toggled.connect(lambda: self.set_stage(1))
        self.rb2.toggled.connect(lambda: self.set_stage(2))
        self.rb3.toggled.connect(lambda: self.set_stage(3))

        group_layout.addWidget(self.rb1)
        group_layout.addWidget(self.rb2)
        group_layout.addWidget(self.rb3)
        group_box.setLayout(group_layout)
        control_layout.addWidget(group_box)

        # Информационное табло
        self.info = QTextEdit()
        self.info.setReadOnly(True)
        control_layout.addWidget(QLabel("Детали объекта:"))
        control_layout.addWidget(self.info)

        # Отрисовка
        self.draw_map()
        # --- КНОПКИ СОХРАНЕНИЯ ---
        self.btn_save_new = QPushButton("Сохранить координаты (Новый файл)")
        self.btn_save_new.clicked.connect(self.save_coords_to_file)
        control_layout.addWidget(self.btn_save_new)

        self.btn_update_json = QPushButton("Обновить viz_data.json")
        self.btn_update_json.setStyleSheet("background-color: #e1f5fe;")
        self.btn_update_json.clicked.connect(self.update_existing_json)
        control_layout.addWidget(self.btn_update_json)

        self.current_stage = 1
        self.viz_links = []
        self.draw_network()
        self.set_stage(1)

        self.btn_open_editor = QPushButton("Открыть редактор сети")
        self.btn_open_editor.clicked.connect(self.open_network_editor)
        control_layout.addWidget(self.btn_open_editor)

    def open_network_editor(self):
        from network_editor import NetworkEditor
        self.editor_window = NetworkEditor()
        self.editor_window.show()

    def parse_osm(self, path):
        try:
            tree = ET.parse(path)
            nodes = {}
            for n in tree.findall(".//node"):
                nodes[n.get('id')] = project_coords(float(n.get('lon')), float(n.get('lat')))
            ways = []
            for w in tree.findall(".//way"):
                if any(t.get('k') == 'highway' for t in w.findall("tag")): # and t.get('v') != 'footway'
                    coords = []
                    for nd in w.findall("nd"):
                        ref = nd.get('ref')
                        if ref in nodes: coords.append(nodes[ref])
                    if len(coords) > 1: ways.append(coords)
            return ways
        except:
            return []

    def load_data(self, path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {}

    def draw_map(self):
        self.scene.addItem(MapBackgroundItem(self.map_data))

    def draw_network(self):
        links = self.traffic_data.get('links', [])
        self.viz_links = []
        node_registry = {}

        for l_data in links:
            c = l_data.get('coords')
            if not c: continue

            # Определяем координаты начала и конца
            if c.get('type') == 'polyline':
                pts = c['points']
                p1_lon, p1_lat = pts[0]
                p2_lon, p2_lat = pts[-1]
            else:
                p1_lon, p1_lat = c['lon_start'], c['lat_start']
                p2_lon, p2_lat = c['lon_end'], c['lat_end']

            p1_raw = project_coords(p1_lon, p1_lat)
            p2_raw = project_coords(p2_lon, p2_lat)

            key1 = (round(p1_raw[0], 1), round(p1_raw[1], 1))
            key2 = (round(p2_raw[0], 1), round(p2_raw[1], 1))

            if key1 not in node_registry:
                node_registry[key1] = TrafficNode(f"N_{len(node_registry)}", QPointF(*p1_raw))
                self.scene.addItem(node_registry[key1])

            if key2 not in node_registry:
                node_registry[key2] = TrafficNode(f"N_{len(node_registry)}", QPointF(*p2_raw))
                self.scene.addItem(node_registry[key2])

            link_item = TrafficLink(l_data, node_registry[key1], node_registry[key2], self.on_link_click)
            self.scene.addItem(link_item)
            self.viz_links.append(link_item)

        self.set_stage(1)
        if self.viz_links:
            self.view.fitInView(self.scene.itemsBoundingRect(), Qt.KeepAspectRatio)

    def set_stage(self, s):
        self.current_stage = s
        for l in self.viz_links: l.update_visuals(s)
        self.info.clear()

    def on_link_click(self, data):
        res = data.get('results', {})
        html = f"<h3>{data.get('name', 'Без названия')}</h3>"
        html += f"<b>ID:</b> {data.get('id')}<br><br>"

        if self.current_stage == 1:  # Режим V/C и LOS
            html += f"<b>Уровень обслуживания (LOS):</b> {res.get('LOS', 'Н/Д')}<br>"
            html += f"<b>Загрузка (V/C):</b> {res.get('VC_ratio', 0)}"

        elif self.current_stage == 2:  # Режим Оптимизации
            prop = res.get('Optimization_Proposal')
            if prop:
                html += f"<font color='red'><b>Предложение:</b> {prop}</font><br><br>"
                html += f"<b>Ожидаемый V/C:</b> {res.get('VC_optimized', 'Н/Д')}<br>"
                html += f"<b>Ожидаемый LOS:</b> {res.get('LOS_optimized', 'Н/Д')}"
            else:
                html += "<font color='green'>Оптимизация не требуется</font>"

        elif self.current_stage == 3:  # Режим Задержки (ВАШ ЗАПРОС)
            delay = res.get('Delay_sec', 0)
            html += f"<font color='#1976d2' size='4'><b>Доп. задержка:</b> {delay} сек.</font><br>"
            html += f"<small>Время, теряемое из-за загрузки участка</small>"

        self.info.setHtml(html)

    # def wheelEvent(self, event: QWheelEvent):
    #     # Зум колесиком мыши
    #     zoom_in_factor = 1.25
    #     zoom_out_factor = 1 / zoom_in_factor
    #     if event.angleDelta().y() > 0:
    #         self.view.scale(zoom_in_factor, zoom_in_factor)
    #     else:
    #         self.view.scale(zoom_out_factor, zoom_out_factor)

    # --- ЛОГИКА СОХРАНЕНИЯ КООРДИНАТ ---

    def get_current_geo_coords(self):
        """Собирает текущие географические координаты всех звеньев."""
        updated_links_coords = {}
        for link in self.viz_links:
            # Получаем экранные координаты узлов
            p1 = link.start_node.scenePos()
            p2 = link.end_node.scenePos()

            # Переводим обратно в lon/lat
            lon_s, lat_s = unproject_coords(p1.x(), p1.y())
            lon_e, lat_e = unproject_coords(p2.x(), p2.y())

            updated_links_coords[link.id] = {
                "lon_start": round(lon_s, 6),
                "lat_start": round(lat_s, 6),
                "lon_end": round(lon_e, 6),
                "lat_end": round(lat_e, 6)
            }
        return updated_links_coords

    def save_coords_to_file(self):
        """Сохраняет только координаты в отдельный файл."""
        coords_data = self.get_current_geo_coords()
        filename = "saved_positions.json"
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(coords_data, f, indent=4, ensure_ascii=False)
            QMessageBox.information(self, "Успех", f"Координаты сохранены в {filename}")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось сохранить файл: {e}")

    def update_existing_json(self):
        """Обновляет координаты прямо в исходном файле viz_data.json."""
        new_coords = self.get_current_geo_coords()

        # Обновляем данные в структуре
        for link in self.traffic_data.get('links', []):
            link_id = link.get('id')
            if link_id in new_coords:
                link['coords'].update(new_coords[link_id])

        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.traffic_data, f, indent=4, ensure_ascii=False)
            QMessageBox.information(self, "Успех", f"Файл {self.data_file} успешно обновлен!")
        except Exception as e:
            QMessageBox.critical(self, "Ошибка", f"Не удалось обновить файл: {e}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(1200, 800)
    window.show()
    sys.exit(app.exec_())