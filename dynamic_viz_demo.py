#!/usr/bin/env python3
"""
Динамическая визуализация транспортных потоков (CTM + СКДФ данные)
Для демонстрации научному руководителю.

Запуск: python dynamic_viz_demo.py --project osm_network_project_skdf_v3.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

try:
    from PyQt5.QtCore import QPointF, QRectF, Qt, QTimer
    from PyQt5.QtGui import QColor, QBrush, QFont, QPainter, QPen, QPolygonF
    from PyQt5.QtWidgets import (
        QApplication, QGraphicsItem, QGraphicsLineItem, QGraphicsPathItem,
        QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QMainWindow,
        QSlider, QVBoxLayout, QWidget, QTextEdit, QPushButton
    )
    PYQT_AVAILABLE = True
except ImportError:
    PYQT_AVAILABLE = False
    # Заглушки для проверки логики без GUI
    class QGraphicsLineItem: pass
    class QGraphicsItem: pass
    class QPointF: pass
    class QRectF: pass
    class Qt: pass
    class QTimer: pass
    class QColor: pass
    class QBrush: pass
    class QFont: pass
    class QPainter: pass
    class QPen: pass
    class QPolygonF: pass
    class QApplication: pass
    class QGraphicsScene: pass
    class QGraphicsView: pass
    class QHBoxLayout: pass
    class QLabel: pass
    class QMainWindow: pass
    class QSlider: pass
    class QVBoxLayout: pass
    class QWidget: pass
    class QTextEdit: pass
    class QPushButton: pass
    print("PyQt5 не найден. Установите: pip install PyQt5")

from ctm import CTMSimulator
from models import Project, SimulationConfig
from network_dynamic import DynamicNetwork


# === КОНФИГУРАЦИЯ ===
DEFAULT_PROJECT = "osm_network_project_skdf_v3.json"
SIMULATION_HORIZON = 360  # 1 час
DT_SECONDS = 10  # шаг симуляции
VIEW_WIDTH = 1200
VIEW_HEIGHT = 800


def load_project(path: str) -> Project:
    """Загружает проект из JSON файла."""
    from project_loader import ProjectLoader
    loader = ProjectLoader()
    return loader.load(path)


def build_dynamic_network_from_project(project: Project) -> DynamicNetwork:
    """Упрощённое построение динамической сети из проекта."""
    from network_dynamic import DynamicLink, Cell
    
    dt = DT_SECONDS
    dynamic_links = {}
    
    # project.network.links может быть dict или list
    if isinstance(project.network.links, dict):
        links_iter = project.network.links.values()
    else:
        links_iter = project.network.links
    
    for link_data in links_iter:
        # Получаем параметры из СКДФ или дефолтные
        metadata = link_data.metadata if hasattr(link_data, 'metadata') else link_data.get('metadata', {})
        skdf = (metadata or {}).get('skdf', {})
        params = link_data.parameters if hasattr(link_data, 'parameters') else link_data.get('parameters', {})
        
        lanes = (skdf.get('lanes') if skdf else None) or (params.get('lanes_total') if params else None) or 2
        speed_limit = (skdf.get('speed_limit') if skdf else None) or (params.get('speed_limit_skdf') if params else None) or 60
        capacity_total = (skdf.get('capacity_total') if skdf else None) or (params.get('capacity_total_skdf') if params else None)
        
        if capacity_total is None:
            # Оценка пропускной способности по полосам
            capacity_total = lanes * 1800
        
        length_km = link_data.length_km if hasattr(link_data, 'length_km') else link_data.get('length_km', 0.1)
        length_m = max(length_km * 1000, 10)
        
        # Разбиваем на ячейки (минимум 1, максимум ~10)
        cell_length = max(length_m / 5, speed_limit / 3.6 * dt)
        cell_count = max(int(math.ceil(length_m / cell_length)), 1)
        
        dynamic_links[link_data.id if hasattr(link_data, 'id') else link_data['id']] = DynamicLink(
            id=link_data.id if hasattr(link_data, 'id') else link_data['id'],
            name=link_data.name if hasattr(link_data, 'name') else link_data.get('name', ''),
            start_node_id=link_data.start_node_id if hasattr(link_data, 'start_node_id') else link_data['start_node_id'],
            end_node_id=link_data.end_node_id if hasattr(link_data, 'end_node_id') else link_data['end_node_id'],
            link_type=link_data.link_type if hasattr(link_data, 'link_type') else link_data.get('link_type', 'urban'),
            length_m=length_m,
            lanes=lanes,
            dt_seconds=dt,
            free_flow_speed_kph=speed_limit,
            wave_speed_kph=20,
            jam_density_pcu_per_km_lane=150,
            capacity_pcu_h=capacity_total,
            cell_length_m=length_m / cell_count,
            parameters=params or {},
            metadata=metadata or {},
            cells=[Cell() for _ in range(cell_count)]
        )
    
    # Создаём движения (упрощённо - все узлы соединяем)
    movements = {}
    node_outgoing = {}
    
    for link_data in links_iter:
        start_id = link_data.start_node_id if hasattr(link_data, 'start_node_id') else link_data['start_node_id']
        if start_id not in node_outgoing:
            node_outgoing[start_id] = []
        link_id = link_data.id if hasattr(link_data, 'id') else link_data['id']
        node_outgoing[start_id].append(link_id)
    
    movement_id = 0
    for node_id, outgoing in node_outgoing.items():
        # Находим входящие ссылки
        incoming = []
        for l in links_iter:
            end_id = l.end_node_id if hasattr(l, 'end_node_id') else l['end_node_id']
            lid = l.id if hasattr(l, 'id') else l['id']
            if end_id == node_id:
                incoming.append(lid)
        
        for from_link in incoming:
            for to_link in outgoing:
                if from_link != to_link:
                    movements[f"mov_{movement_id}"] = {
                        'id': f"mov_{movement_id}",
                        'from_link_id': from_link,
                        'to_link_id': to_link,
                        'split_ratio': 1.0 / max(len(outgoing), 1),
                        'capacity_pcu_h': None,
                        'control': {'control_type': 'uncontrolled'}
                    }
                    movement_id += 1
    
    # Источники и стоки (упрощённо - граничные ссылки)
    sources = {}
    sinks = {}
    
    in_degree = {}
    out_degree = {}
    for link_data in links_iter:
        start = link_data.start_node_id if hasattr(link_data, 'start_node_id') else link_data['start_node_id']
        end = link_data.end_node_id if hasattr(link_data, 'end_node_id') else link_data['end_node_id']
        out_degree[start] = out_degree.get(start, 0) + 1
        in_degree[end] = in_degree.get(end, 0) + 1
    
    source_id = 0
    sink_id = 0
    for link_data in links_iter:
        lid = link_data.id if hasattr(link_data, 'id') else link_data['id']
        start = link_data.start_node_id if hasattr(link_data, 'start_node_id') else link_data['start_node_id']
        end = link_data.end_node_id if hasattr(link_data, 'end_node_id') else link_data['end_node_id']
        
        if in_degree.get(start, 0) == 0:
            sources[f"src_{source_id}"] = {
                'id': f"src_{source_id}",
                'link_id': lid,
                'demand_by_type': {'car': 500},
                'start_time_s': 0,
                'end_time_s': SIMULATION_HORIZON
            }
            source_id += 1
        
        if out_degree.get(end, 0) == 0:
            sinks[f"snk_{sink_id}"] = {
                'id': f"snk_{sink_id}",
                'link_id': lid,
                'capacity_pcu_h': 2000
            }
            sink_id += 1
    
    # project.network.nodes может быть dict или list
    if isinstance(project.network.nodes, dict):
        nodes = project.network.nodes
    else:
        nodes = {n.id if hasattr(n, 'id') else n['id']: n for n in project.network.nodes}
    
    return DynamicNetwork(
        nodes=nodes,
        links=dynamic_links,
        sources=sources,
        sinks=sinks,
        movements=movements,
        diagnostics=[]
    )


def get_los_color(density_ratio: float) -> QColor:
    """Цвет по уровню загрузки (LOS)."""
    if density_ratio < 0.35:
        return QColor(0, 200, 0)    # A - зелёный
    elif density_ratio < 0.55:
        return QColor(100, 220, 100) # B - светло-зелёный
    elif density_ratio < 0.70:
        return QColor(255, 255, 0)   # C - жёлтый
    elif density_ratio < 0.85:
        return QColor(255, 165, 0)   # D - оранжевый
    elif density_ratio < 1.0:
        return QColor(255, 69, 0)    # E - красно-оранжевый
    else:
        return QColor(255, 0, 0)     # F - красный


class DynamicLinkItem(QGraphicsLineItem):
    def __init__(self, link_id: str, start_pos: QPointF, end_pos: QPointF, 
                 base_capacity: float, length_m: float):
        super().__init__(start_pos.x(), start_pos.y(), end_pos.x(), end_pos.y())
        self.link_id = link_id
        self.base_capacity = base_capacity
        self.length_m = length_m
        self.current_density = 0.0
        self.setPen(QPen(QColor(150, 150, 150), 4, Qt.SolidLine, Qt.RoundCap))
        self.setZValue(10)
        
    def update_color(self, density_ratio: float):
        color = get_los_color(density_ratio)
        width = max(4, int(4 + density_ratio * 6))
        self.setPen(QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))


class NodeItem(QGraphicsItem):
    def __init__(self, node_id: str, pos: QPointF):
        super().__init__()
        self.node_id = node_id
        self.setPos(pos)
        self.setZValue(20)
        
    def boundingRect(self):
        return QRectF(-6, -6, 12, 12)
    
    def paint(self, painter, option, widget):
        painter.setBrush(QBrush(QColor(50, 50, 100)))
        painter.setPen(QPen(Qt.black, 1))
        painter.drawEllipse(-5, -5, 10, 10)


class DynamicVizWindow(QMainWindow):
    def __init__(self, project: Project, network: DynamicNetwork):
        super().__init__()
        self.setWindowTitle("Динамическая визуализация транспортных потоков (CTM)")
        self.resize(VIEW_WIDTH, VIEW_HEIGHT)
        
        self.project = project
        self.network = network
        self.simulator = CTMSimulator(network, SimulationConfig(
            dt_seconds=DT_SECONDS,
            horizon_seconds=SIMULATION_HORIZON,
            min_dt_seconds=5,
            max_dt_seconds=60,
            adaptive_dt_enabled=False
        ))
        
        # Предварительно считаем все шаги симуляции
        print("Запуск симуляции...")
        self.all_states = self._run_full_simulation()
        print(f"Симуляция завершена. Шагов: {len(self.all_states)}")
        
        self.current_step = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.next_frame)
        
        # Создаём сцену и вид
        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        
        # Отрисовываем сеть
        self.link_items = {}
        self.node_positions = {}
        self._build_scene()
        
        # Панель управления
        control_panel = QWidget()
        control_layout = QVBoxLayout(control_panel)
        
        self.time_label = QLabel("Время: 0 / {} сек".format(SIMULATION_HORIZON))
        control_layout.addWidget(self.time_label)
        
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, len(self.all_states) - 1)
        self.slider.valueChanged.connect(self.on_slider_change)
        control_layout.addWidget(self.slider)
        
        self.btn_play = QPushButton("▶ Старт")
        self.btn_play.clicked.connect(self.toggle_play)
        control_layout.addWidget(self.btn_play)
        
        self.info_text = QTextEdit()
        self.info_text.setReadOnly(True)
        self.info_text.setMaximumHeight(150)
        control_layout.addWidget(QLabel("Статистика:"))
        control_layout.addWidget(self.info_text)
        
        # Компоновка
        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addWidget(self.view, 4)
        layout.addWidget(control_panel, 1)
        self.setCentralWidget(central)
        
        # Обновляем первый кадр
        self.update_frame(0)
    
    def _run_full_simulation(self) -> list[dict]:
        """Запускает симуляцию и сохраняет состояние на каждом шаге."""
        states = []
        horizon = SIMULATION_HORIZON
        steps = horizon // DT_SECONDS
        
        # Сбрасываем состояния ячеек
        for link in self.network.links.values():
            for cell in link.cells:
                cell.occupancy_pcu = 0.0
        
        for step in range(steps + 1):
            current_time = step * DT_SECONDS
            
            # Сохраняем состояние
            state = {}
            for link_id, link in self.network.links.items():
                total_occupancy = sum(c.occupancy_pcu for c in link.cells)
                max_density = link.lanes * link.jam_density_pcu_per_km_lane * (link.length_m / 1000)
                density_ratio = total_occupancy / max(max_density, 0.001)
                state[link_id] = {
                    'occupancy': total_occupancy,
                    'density_ratio': min(density_ratio, 1.5)
                }
            states.append(state)
            
            if step < steps:
                # Один шаг симуляции
                internal_flows = self.simulator._compute_internal_flows()
                node_flows = self.simulator._compute_node_flows(current_time)
                sink_flows = self.simulator._compute_sink_flows()
                source_flows = self.simulator._compute_source_flows(current_time)
                
                for link_id, link in self.network.links.items():
                    inflows = [0.0] * len(link.cells)
                    outflows = [0.0] * len(link.cells)
                    
                    for cell_idx, flow in internal_flows.get(link_id, {}).items():
                        outflows[cell_idx] += flow
                        if cell_idx + 1 < len(inflows):
                            inflows[cell_idx + 1] += flow
                    
                    node_out = sum(
                        flow for mid, flow in node_flows.items()
                        if self.network.movements[mid]['from_link_id'] == link_id
                    )
                    node_in = sum(
                        flow for mid, flow in node_flows.items()
                        if self.network.movements[mid]['to_link_id'] == link_id
                    )
                    
                    if link.cells:
                        outflows[-1] += node_out + sink_flows.get(link_id, 0.0)
                        inflows[0] += node_in + source_flows.get(link_id, 0.0)
                    
                    for cell_idx, cell in enumerate(link.cells):
                        cell.occupancy_pcu = max(
                            cell.occupancy_pcu + inflows[cell_idx] - outflows[cell_idx],
                            0.0
                        )
        
        return states
    
    def _build_scene(self):
        """Отрисовывает сеть на сцене."""
        # Считаем координаты узлов (упрощённо - раскладываем по кругу)
        nodes = list(self.network.nodes.keys())
        n = len(nodes)
        if n == 0:
            return
        
        radius = min(VIEW_WIDTH, VIEW_HEIGHT) * 0.35
        center = QPointF(VIEW_WIDTH / 2, VIEW_HEIGHT / 2)
        
        for i, node_id in enumerate(nodes):
            angle = 2 * math.pi * i / n
            x = center.x() + radius * math.cos(angle)
            y = center.y() + radius * math.sin(angle)
            pos = QPointF(x, y)
            self.node_positions[node_id] = pos
            self.scene.addItem(NodeItem(node_id, pos))
        
        # Рисуем ссылки
        for link_id, link in self.network.links.items():
            start = self.node_positions.get(link.start_node_id)
            end = self.node_positions.get(link.end_node_id)
            if start and end:
                item = DynamicLinkItem(
                    link_id, start, end,
                    link.capacity_pcu_h, link.length_m
                )
                self.link_items[link_id] = item
                self.scene.addItem(item)
    
    def update_frame(self, step: int):
        """Обновляет визуализацию на указанном шаге."""
        if step >= len(self.all_states):
            return
        
        self.current_step = step
        state = self.all_states[step]
        time_sec = step * DT_SECONDS
        
        # Обновляем цвета ссылок
        total_vehicles = 0
        for link_id, item in self.link_items.items():
            link_state = state.get(link_id, {'density_ratio': 0})
            density_ratio = link_state['density_ratio']
            total_vehicles += link_state['occupancy']
            item.update_color(density_ratio)
        
        # Обновляем метки
        self.time_label.setText(f"Время: {time_sec} / {SIMULATION_HORIZON} сек")
        self.slider.setValue(step)
        
        self.info_text.setText(
            f"Шаг: {step}\n"
            f"Время: {time_sec} сек ({time_sec // 60} мин)\n"
            f"Автомобилей в сети: {int(total_vehicles)}\n"
            f"Сегментов: {len(self.link_items)}"
        )
    
    def next_frame(self):
        self.current_step = (self.current_step + 1) % len(self.all_states)
        self.update_frame(self.current_step)
    
    def on_slider_change(self, value):
        self.update_frame(value)
        if self.timer.isActive():
            self.timer.stop()
            self.btn_play.setText("▶ Старт")
    
    def toggle_play(self):
        if self.timer.isActive():
            self.timer.stop()
            self.btn_play.setText("▶ Старт")
        else:
            self.timer.start(100)  # 10 FPS
            self.btn_play.setText("⏸ Пауза")


def main():
    parser = argparse.ArgumentParser(description="Динамическая визуализация CTM")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Путь к JSON проекта")
    args = parser.parse_args()
    
    if not PYQT_AVAILABLE:
        print("Ошибка: PyQt5 не установлен.")
        return 1
    
    project_path = Path(args.project)
    if not project_path.exists():
        print(f"Файл проекта не найден: {project_path}")
        return 1
    
    print(f"Загрузка проекта: {project_path}")
    project = load_project(str(project_path))
    print(f"Загружено {len(project.network.links)} сегментов")
    
    print("Построение динамической сети...")
    network = build_dynamic_network_from_project(project)
    print(f"Создано {len(network.links)} динамических ссылок")
    
    app = QApplication([])
    window = DynamicVizWindow(project, network)
    window.show()
    
    return app.exec_()


if __name__ == "__main__":
    exit(main())
