import os
import sys
import xml.etree.ElementTree as ET
import math

from PyQt5.QtCore import QPointF, QRectF, Qt
from PyQt5.QtGui import QColor, QBrush, QFont, QPainter, QPainterPath, QPen, QPolygonF, QWheelEvent
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


def get_los_and_color_from_density(density_per_lane):
    if density_per_lane <= 11: return "A", QColor(0, 200, 0)
    if density_per_lane <= 16: return "B", QColor(100, 220, 100)
    if density_per_lane <= 22: return "C", QColor(255, 255, 0)
    if density_per_lane <= 28: return "D", QColor(255, 165, 0)
    if density_per_lane <= 35: return "E", QColor(255, 69, 0)
    return "F", QColor(255, 0, 0)


NODE_COLORS = {
    "boundary": QColor(220, 40, 40),
    "intersection": QColor(45, 90, 210),
    "roundabout_part": QColor(150, 70, 210),
    "ordinary": QColor(80, 80, 80),
}


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


# The rest of the visualizer is intentionally imported from the historical file
# to keep this patch small.  The actual fixed MainWindow below subclasses the
# original implementation and corrects time labels after loading the project.
from traffic_viz_test import MainWindow as _OriginalMainWindow  # type: ignore  # noqa: E402


class MainWindow(_OriginalMainWindow):
    def _snapshot_interval_sec(self):
        if not self.project:
            return 60
        config = self.project.metadata.get("ctm_scenario_config", {}) or {}
        return int(config.get("snapshot_interval_sec", 60) or 60)

    def _time_label(self, index):
        minutes = index * self._snapshot_interval_sec() / 60.0
        if abs(minutes - round(minutes)) < 1e-9:
            return f"Время: {int(round(minutes))} мин"
        return f"Время: {minutes:.1f} мин"

    def on_time_changed(self, value):
        self.current_time_index = value
        self.lbl_time.setText(self._time_label(value))
        for link in self.viz_links:
            link.update_visuals(self.current_time_index)
        if hasattr(self, 'last_clicked_link') and self.last_clicked_link:
            self.on_link_click(self.last_clicked_link)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())
