import math
import os
import sys
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

# from analysis_service import AnalysisService
from project_loader import ProjectLoader
from project_saver import ProjectSaver
# from routing_service import RoutingService

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
        self.node_id = node_model.id
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
        self.is_route_highlighted = False
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
        OFFSET_PX = 4.0  # Сдвиг в пикселях. Можете сделать 5 или 6, если хочется шире
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
                dx = base_points[i + 1].x() - base_points[i - 1].x()
                dy = base_points[i + 1].y() - base_points[i - 1].y()

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

    # ПЕРЕОПРЕДЕЛИ boundingRect, чтобы добавить запас для стрелок
    def boundingRect(self):
        # Берем базовый размер (путь + толщина пера)
        base_rect = super().boundingRect()
        # Раздуваем его на несколько пикселей во все стороны для запаса под стрелку
        return base_rect.adjusted(-5, -5, 5, 5)

    def update_visuals(self, stage):
        res = self.link_model.results or {}
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

    def paint(self, painter, option, widget):
        # 1. Сначала рисуем саму линию дороги (стандартное поведение)
        super().paint(painter, option, widget)

        # 2. Рисуем стрелку направления
        path = self.path()
        if path.length() < 15:  # Не рисуем стрелку на слишком коротких отрезках
            return

        # Определяем положение стрелки (например, на 90% длины пути, ближе к концу)
        percent = 0.9
        point = path.pointAtPercent(percent)
        angle = path.angleAtPercent(percent)  # Угол наклона пути в этой точке

        # Настраиваем кисть для стрелки (используем цвет линии)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        
        # Цвет стрелки берем из текущего пера, но делаем его непрозрачным
        arrow_color = self.pen().color()
        arrow_color.setAlpha(255) 
        painter.setBrush(QBrush(arrow_color))
        painter.setPen(Qt.NoPen)

        # Перемещаем систему координат в точку на линии и поворачиваем
        painter.translate(point)
        painter.rotate(-angle) # В Qt углы идут по часовой стрелке, инвертируем

        # Рисуем треугольник (стрелку)
        # Размер стрелки зависит от ширины дороги
        arrow_size = self.pen().width() * 1.3
        arrow_head = QPolygonF([
            QPointF(arrow_size, 0),                # Носик стрелки
            QPointF(-arrow_size, -arrow_size * 0.6), # Верхнее "крыло"
            QPointF(-arrow_size, arrow_size * 0.6)   # Нижнее "крыло"
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
        self.setWindowTitle("Транспортный визуализатор")
        self.resize(1400, 900)

        self.loader = ProjectLoader()
        self.saver = ProjectSaver()
        # self.analysis_service = AnalysisService()
        # self.routing_service = RoutingService()
        if not os.path.exists(data_file) and os.path.exists("manual_network.json"):
            data_file = "manual_network.json"
        self.data_file = data_file
        self.project = None
        self.map_data = self.parse_osm(map_file)
        self.demand_report_text = ""
        self.viz_links = []
        self.link_index = {}
        self.node_index = {}
        self.current_stage = 1

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

        # self.btn_demand_wizard = QPushButton("Автогенерация demand_model")
        # self.btn_demand_wizard.clicked.connect(self.open_demand_model_wizard)
        # control_layout.addWidget(self.btn_demand_wizard)

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

    # def open_demand_model_wizard(self):
    #     try:
    #         # from demand_model_wizard import DemandModelWizard
    #     except Exception as exc:
    #         QMessageBox.critical(self, "Demand model", f"Не удалось открыть мастер:\n{exc}")
    #         return
    #     dialog = DemandModelWizard(self.data_file, self)
    #     if dialog.exec_():
    #         self.reload_project_and_redraw()

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
        self.link_index = {}
        self.node_index = {}
        self.demand_report_text = ""
        self.draw_map()
        self.load_project_data(self.data_file)
        self.draw_network()
        self.set_stage(1)

    def parse_osm(self, path):
        try:
            tree = ET.parse(path)
            nodes = {}
            for n in tree.findall(".//node"):
                nodes[n.get("id")] = project_coords(float(n.get("lon")), float(n.get("lat")))
            roads = []
            buildings = []
            allowed_highways = {"primary", "secondary", "tertiary", "residential"}
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
            # needs_analysis = self._has_demand_model() or any(not link.results for link in self.project.network.links.values())
            # if needs_analysis:
            #     report = self.analysis_service.analyze_project(self.project)
            #     self._show_demand_report(report)
        except Exception as exc:
            QMessageBox.critical(self, "Ошибка", f"Не удалось загрузить проект: {exc}")
            self.project = None

    def _show_demand_report(self, report):
        status = report.get("Analysis_Status")
        assignment_report = report.get("Demand_Assignment", {})
        text = self._format_demand_summary(status, assignment_report, report)
        self.demand_report_text = text
        self.info.setPlainText(text)
        if status in {"Validation failed", "Demand assignment failed"}:
            QMessageBox.critical(self, "Demand assignment", text)
            return
        warnings = assignment_report.get("warnings", []) or []
        scenario_warnings = self._scenario_warnings()
        if warnings or scenario_warnings:
            QMessageBox.warning(self, "Demand assignment warnings", text)

    def _format_demand_summary(self, status, assignment_report, analysis_report=None):
        return "\n".join(self._demand_summary_lines(status, assignment_report, analysis_report or {}))

    def _demand_summary_lines(self, status, assignment_report, analysis_report):
        demand_model = self.project.demand_model or {}
        lines = [
            "Demand assignment",
            f"demand_model.type: {demand_model.get('type')}",
            f"status: {status}",
            f"assigned_routes: {assignment_report.get('assigned_routes', 0)}",
        ]
        for origin, summary in assignment_report.get("boundary_flow_summary", {}).items():
            lines.append(
                f"{origin}: boundary={summary.get('boundary_flow')}, "
                f"assigned={summary.get('assigned_flow')}, unassigned={summary.get('unassigned_flow')}"
            )
        validation_errors = analysis_report.get("Validation_Errors", []) or []
        assignment_errors = assignment_report.get("errors", []) or []
        warnings = assignment_report.get("warnings", []) or []
        scenario_warnings = self._scenario_warnings()
        if validation_errors or assignment_errors:
            lines.append("")
            lines.append("errors:")
            lines.extend(f"- {error}" for error in validation_errors)
            lines.extend(f"- {error}" for error in assignment_errors)
        if warnings or scenario_warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"- {warning}" for warning in warnings)
            lines.extend(f"- {warning}" for warning in scenario_warnings)
        return lines

    def _scenario_warnings(self):
        if self.project is None:
            return []
        return self.project.metadata.get("scenario_warnings", []) or []

    def _has_demand_model(self):
        return bool(self.project and self.project.demand_model)

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
            self.node_index[node_model.id] = node_item

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

    def set_stage(self, stage):
        self.current_stage = stage
        for link in self.viz_links:
            link.update_visuals(stage)
        if self.demand_report_text:
            self.info.setPlainText(self.demand_report_text)
        else:
            self.info.clear()

    def on_node_click(self, node_model):
        incoming = self.project.network.get_incoming_links(node_model.id)
        outgoing = self.project.network.get_outgoing_links(node_model.id)
        html = f"<h3>{node_model.name or node_model.id}</h3>"
        html += f"<b>ID:</b> {node_model.id}<br>"
        html += f"<b>Тип:</b> {node_model.node_type}<br>"
        html += f"<b>lon/lat:</b> {node_model.lon}, {node_model.lat}<br>"
        html += f"<b>Входящих link:</b> {len(incoming)}<br>"
        html += f"<b>Исходящих link:</b> {len(outgoing)}<br>"
        if node_model.metadata:
            html += "<br><b>metadata:</b><br>"
            for key, value in sorted(node_model.metadata.items()):
                html += f"{key}: {value}<br>"
        html += "<br><b>incoming:</b><br>" + "<br>".join(link.id for link in incoming[:20])
        html += "<br><br><b>outgoing:</b><br>" + "<br>".join(link.id for link in outgoing[:20])
        self.info.setHtml(html)

    def on_link_click(self, link_model):
        res = link_model.results or {}
        html = f"<h3>{link_model.name}</h3>"
        html += f"<b>ID:</b> {link_model.id}<br>"
        html += f"<b>from → to:</b> {link_model.start_node_id} → {link_model.end_node_id}<br>"
        html += f"<b>Тип:</b> {link_model.link_type}<br>"
        html += f"<b>Длина:</b> {link_model.length_km} км<br>"
        html += f"<b>Поток:</b> {link_model.traffic_counts}<br><br>"
        if self.current_stage == 1:
            html += f"<b>LOS:</b> {res.get('LOS', 'Н/Д')}<br>"
            html += f"<b>V/C:</b> {res.get('VC_ratio', 0)}<br>"
            html += f"<b>V:</b> {res.get('V', 'Н/Д')}<br>"
            html += f"<b>C:</b> {res.get('C_initial', 'Н/Д')}<br>"
        elif self.current_stage == 2:
            html += f"<b>Optimization:</b> {res.get('Optimization_Proposal') or 'не требуется'}<br>"
        elif self.current_stage == 3:
            html += f"<b>Delay_sec:</b> {res.get('Delay_sec', 0)}<br>"
        else:
            html += "<b>metadata:</b><br>"
            for key, value in sorted((link_model.metadata or {}).items()):
                html += f"{key}: {value}<br>"
        self.info.setHtml(html)

    def find_route(self):
        if self.project is None or not self.project.network.nodes:
            QMessageBox.warning(self, "Маршрут", "Проект не загружен.")
            return
        node_display = self._get_node_display_pairs()
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
        path_link_ids = self.routing_service.find_shortest_path(
            self.project.network,
            display_to_id[start_display],
            display_to_id[end_display],
            weight,
        )
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
        ordered_nodes = sorted(self.project.network.nodes.values(), key=lambda node: node.name or node.id)
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
