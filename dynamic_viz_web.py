#!/usr/bin/env python3
"""
Динамическая визуализация транспортных потоков (CTM + СКДФ данные) - Веб-версия
Для демонстрации научному руководителю.

Запуск: python dynamic_viz_web.py --project osm_network_project_skdf_v3.json
Откроет HTML файл в браузере.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from ctm import CTMSimulator
from models import Project, SimulationConfig
from network_dynamic import DynamicNetwork, DynamicLink, Cell


# === КОНФИГУРАЦИЯ ===
DEFAULT_PROJECT = "osm_network_project_skdf_v3.json"
SIMULATION_HORIZON = 600  # 10 минут для быстрой демонстрации
DT_SECONDS = 30  # шаг симуляции 30 секунд для ускорения
VIEW_WIDTH = 1200
VIEW_HEIGHT = 800
MAX_LINKS_FOR_DEMO = 150  # Ограничение количества сегментов для демонстрации


def load_project(path: str) -> Project:
    """Загружает проект из JSON файла."""
    from project_loader import ProjectLoader
    loader = ProjectLoader()
    return loader.load(path)


def build_dynamic_network_from_project(project: Project) -> DynamicNetwork:
    """Упрощённое построение динамической сети из проекта."""
    dt = DT_SECONDS
    dynamic_links = {}

    # project.network.links может быть dict или list
    if isinstance(project.network.links, dict):
        links_iter = list(project.network.links.values())
    else:
        links_iter = list(project.network.links)

    # Ограничиваем количество сегментов для быстрой демонстрации
    if len(links_iter) > MAX_LINKS_FOR_DEMO:
        links_iter = links_iter[:MAX_LINKS_FOR_DEMO]

    for link_data in links_iter:
        # Получаем параметры из СКДФ или дефолтные
        metadata = link_data.metadata if hasattr(link_data, 'metadata') else link_data.get('metadata', {})
        skdf = (metadata or {}).get('skdf', {})
        params = link_data.parameters if hasattr(link_data, 'parameters') else link_data.get('parameters', {})

        lanes = (skdf.get('lanes') if skdf else None) or (params.get('lanes_total') if params else None) or 2
        speed_limit = (skdf.get('speed_limit') if skdf else None) or (params.get('speed_limit_skdf') if params else None) or 60
        capacity_total = (skdf.get('capacity_total') if skdf else None) or (params.get('capacity_total_skdf') if params else None)

        if capacity_total is None:
            capacity_total = lanes * 1800

        length_km = link_data.length_km if hasattr(link_data, 'length_km') else link_data.get('length_km', 0.1)
        length_m = max(length_km * 1000, 10)

        cell_length = max(length_m / 5, speed_limit / 3.6 * dt)
        cell_count = max(int(math.ceil(length_m / cell_length)), 1)

        lid = link_data.id if hasattr(link_data, 'id') else link_data['id']
        dynamic_links[lid] = DynamicLink(
            id=lid,
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

    # Создаём движения
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

    # Источники и стоки
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


def run_simulation(network: DynamicNetwork) -> list[dict]:
    """Запускает симуляцию и возвращает состояния на каждом шаге."""
    from models import Movement, Source, Sink

    # Преобразуем все словари в объекты dataclass ДО создания симулятора
    for mid, m in list(network.movements.items()):
        if isinstance(m, dict):
            to_link = network.links.get(m.get('to_link_id', ''))
            node_id = to_link.start_node_id if to_link else m.get('node_id', '')
            network.movements[mid] = Movement(
                id=m.get('id', mid),
                node_id=node_id,
                from_link_id=m.get('from_link_id', ''),
                to_link_id=m.get('to_link_id', ''),
                split_ratio=m.get('split_ratio', 1.0),
                capacity_pcu_h=m.get('capacity_pcu_h'),
                control=m.get('control', {}),
                inferred=m.get('inferred', False),
                metadata=m.get('metadata', {})
            )

    for sid, s in list(network.sources.items()):
        if isinstance(s, dict):
            network.sources[sid] = Source(
                id=s.get('id', sid),
                link_id=s.get('link_id', ''),
                demand_by_type=s.get('demand_by_type', {}),
                start_time_s=s.get('start_time_s', 0),
                end_time_s=s.get('end_time_s'),
                inferred=s.get('inferred', False),
                metadata=s.get('metadata', {})
            )

    for sid, s in list(network.sinks.items()):
        if isinstance(s, dict):
            network.sinks[sid] = Sink(
                id=s.get('id', sid),
                link_id=s.get('link_id', ''),
                capacity_pcu_h=s.get('capacity_pcu_h'),
                inferred=s.get('inferred', False),
                metadata=s.get('metadata', {})
            )

    simulator = CTMSimulator(network, SimulationConfig(
        dt_seconds=DT_SECONDS,
        horizon_seconds=SIMULATION_HORIZON,
        min_dt_seconds=5,
        max_dt_seconds=60,
        adaptive_dt_enabled=False
    ))

    states = []
    horizon = SIMULATION_HORIZON
    steps = horizon // DT_SECONDS

    for step in range(steps + 1):
        current_time = step * DT_SECONDS

        state = {}
        for link_id, link in network.links.items():
            total_occupancy = sum(c.occupancy_pcu for c in link.cells)
            max_density = link.lanes * link.jam_density_pcu_per_km_lane * (link.length_m / 1000)
            density_ratio = total_occupancy / max(max_density, 0.001)
            state[link_id] = {
                'occupancy': total_occupancy,
                'density_ratio': min(density_ratio, 1.5)
            }
        states.append(state)

        if step < steps:
            internal_flows = simulator._compute_internal_flows()
            node_flows = simulator._compute_node_flows(current_time)
            sink_flows = simulator._compute_sink_flows()
            source_flows = simulator._compute_source_flows(current_time)

            for link_id, link in network.links.items():
                inflows = [0.0] * len(link.cells)
                outflows = [0.0] * len(link.cells)

                for cell_idx, flow in internal_flows.get(link_id, {}).items():
                    outflows[cell_idx] += flow
                    if cell_idx + 1 < len(inflows):
                        inflows[cell_idx + 1] += flow

                node_out = sum(
                    flow for mid, flow in node_flows.items()
                    if network.movements[mid].from_link_id == link_id
                )
                node_in = sum(
                    flow for mid, flow in node_flows.items()
                    if network.movements[mid].to_link_id == link_id
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


def get_los_color(density_ratio: float) -> str:
    """Возвращает HEX цвет по уровню загрузки."""
    if density_ratio < 0.35:
        return "#00c800"  # A - зелёный
    elif density_ratio < 0.55:
        return "#64dc64"  # B - светло-зелёный
    elif density_ratio < 0.70:
        return "#ffff00"  # C - жёлтый
    elif density_ratio < 0.85:
        return "#ffa500"  # D - оранжевый
    elif density_ratio < 1.0:
        return "#ff4500"  # E - красно-оранжевый
    else:
        return "#ff0000"  # F - красный


def build_visualization_html(project: Project, network: DynamicNetwork, states: list[dict]) -> str:
    """Генерирует HTML с интерактивной визуализацией."""

    # Вычисляем позиции узлов (раскладываем по кругу для простоты)
    nodes_list = list(network.nodes.keys())
    n = len(nodes_list)
    radius = min(VIEW_WIDTH, VIEW_HEIGHT) * 0.35
    center_x, center_y = VIEW_WIDTH / 2, VIEW_HEIGHT / 2

    node_positions = {}
    for i, node_id in enumerate(nodes_list):
        angle = 2 * math.pi * i / n
        x = center_x + radius * math.cos(angle)
        y = center_y + radius * math.sin(angle)
        node_positions[node_id] = (x, y)

    # Подготовка данных о ссылках
    links_data = []
    for link_id, link in network.links.items():
        start_pos = node_positions.get(link.start_node_id, (center_x, center_y))
        end_pos = node_positions.get(link.end_node_id, (center_x + 50, center_y))

        # Собираем состояния по шагам
        link_states = [states[step].get(link_id, {'density_ratio': 0})['density_ratio'] for step in range(len(states))]

        links_data.append({
            'id': link_id,
            'name': link.name,
            'start': start_pos,
            'end': end_pos,
            'capacity': link.capacity_pcu_h,
            'length_m': link.length_m,
            'lanes': link.lanes,
            'states': link_states
        })

    # Данные для JavaScript
    links_json = json.dumps(links_data, ensure_ascii=False)
    nodes_json = json.dumps([{'id': nid, 'pos': node_positions[nid]} for nid in nodes_list], ensure_ascii=False)
    total_steps = len(states)

    html = f'''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Динамическая визуализация транспортных потоков (CTM)</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
        .container {{ display: flex; gap: 20px; }}
        #canvas-container {{ background: white; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 20px; }}
        canvas {{ border: 1px solid #ddd; border-radius: 4px; }}
        .controls {{ flex: 1; max-width: 350px; background: white; border-radius: 8px; padding: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        h1 {{ margin: 0 0 20px 0; color: #333; font-size: 20px; }}
        .stat {{ margin: 10px 0; padding: 10px; background: #f9f9f9; border-radius: 4px; }}
        .stat-label {{ font-weight: bold; color: #666; }}
        .stat-value {{ font-size: 18px; color: #333; }}
        input[type="range"] {{ width: 100%; margin: 10px 0; }}
        button {{ background: #4CAF50; color: white; border: none; padding: 12px 24px; border-radius: 4px; cursor: pointer; font-size: 16px; width: 100%; margin-top: 10px; }}
        button:hover {{ background: #45a049; }}
        button.paused {{ background: #ff9800; }}
        button.paused:hover {{ background: #e68900; }}
        .legend {{ margin-top: 20px; }}
        .legend-item {{ display: flex; align-items: center; margin: 5px 0; }}
        .legend-color {{ width: 20px; height: 20px; margin-right: 10px; border-radius: 3px; }}
        .info-box {{ margin-top: 20px; padding: 15px; background: #e3f2fd; border-radius: 4px; font-size: 13px; }}
    </style>
</head>
<body>
    <h1>🚦 Динамическая визуализация транспортных потоков (CTM + данные СКДФ)</h1>
    <div class="container">
        <div id="canvas-container">
            <canvas id="vizCanvas" width="{VIEW_WIDTH}" height="{VIEW_HEIGHT}"></canvas>
        </div>
        <div class="controls">
            <div class="stat">
                <div class="stat-label">Время симуляции:</div>
                <div class="stat-value"><span id="timeDisplay">0</span> / {SIMULATION_HORIZON} сек</div>
            </div>
            <div class="stat">
                <div class="stat-label">Шаг:</div>
                <div class="stat-value"><span id="stepDisplay">0</span> / {total_steps - 1}</div>
            </div>
            <div class="stat">
                <div class="stat-label">Автомобилей в сети:</div>
                <div class="stat-value" id="vehiclesDisplay">0</div>
            </div>
            <div class="stat">
                <div class="stat-label">Сегментов дорог:</div>
                <div class="stat-value">{len(links_data)}</div>
            </div>

            <label for="timeSlider">Перемещайте ползунок:</label>
            <input type="range" id="timeSlider" min="0" max="{total_steps - 1}" value="0">

            <button id="playBtn" onclick="togglePlay()">▶ Старт</button>

            <div class="legend">
                <strong>Уровень загрузки (LOS):</strong>
                <div class="legend-item"><div class="legend-color" style="background: #00c800;"></div>A: Свободно (&lt;35%)</div>
                <div class="legend-item"><div class="legend-color" style="background: #64dc64;"></div>B: Почти свободно (35-55%)</div>
                <div class="legend-item"><div class="legend-color" style="background: #ffff00;"></div>C: Умеренно (55-70%)</div>
                <div class="legend-item"><div class="legend-color" style="background: #ffa500;"></div>D: Нагрузка (70-85%)</div>
                <div class="legend-item"><div class="legend-color" style="background: #ff4500;"></div>E: Пробка (85-100%)</div>
                <div class="legend-item"><div class="legend-color" style="background: #ff0000;"></div>F: Затор (&gt;100%)</div>
            </div>

            <div class="info-box">
                <strong>О модели:</strong><br>
                Используется макроскопическая гидродинамическая модель LWR/CTM.<br>
                Параметры сегментов взяты из данных СКДФ.рф:<br>
                • Разрешённая скорость<br>
                • Пропускная способность<br>
                • Количество полос
            </div>
        </div>
    </div>

    <script>
        const links = {links_json};
        const nodes = {nodes_json};
        const totalSteps = {total_steps};
        const horizon = {SIMULATION_HORIZON};
        const dt = {DT_SECONDS};

        let currentStep = 0;
        let isPlaying = false;
        let playInterval = null;

        const canvas = document.getElementById('vizCanvas');
        const ctx = canvas.getContext('2d');
        const slider = document.getElementById('timeSlider');
        const playBtn = document.getElementById('playBtn');
        const timeDisplay = document.getElementById('timeDisplay');
        const stepDisplay = document.getElementById('stepDisplay');
        const vehiclesDisplay = document.getElementById('vehiclesDisplay');

        function getLosColor(densityRatio) {{
            if (densityRatio < 0.35) return '#00c800';
            if (densityRatio < 0.55) return '#64dc64';
            if (densityRatio < 0.70) return '#ffff00';
            if (densityRatio < 0.85) return '#ffa500';
            if (densityRatio < 1.0) return '#ff4500';
            return '#ff0000';
        }}

        function draw() {{
            ctx.clearRect(0, 0, canvas.width, canvas.height);

            // Рисуем ссылки
            let totalVehicles = 0;
            links.forEach(link => {{
                const state = link.states[currentStep] || 0;
                totalVehicles += (link.states[currentStep] || 0) * link.lanes * link.length_m / 1000 * 150;

                const color = getLosColor(state);
                const width = Math.max(3, 3 + state * 8);

                ctx.beginPath();
                ctx.moveTo(link.start[0], link.start[1]);
                ctx.lineTo(link.end[0], link.end[1]);
                ctx.strokeStyle = color;
                ctx.lineWidth = width;
                ctx.lineCap = 'round';
                ctx.stroke();
            }});

            // Рисуем узлы
            nodes.forEach(node => {{
                ctx.beginPath();
                ctx.arc(node.pos[0], node.pos[1], 6, 0, Math.PI * 2);
                ctx.fillStyle = '#323296';
                ctx.fill();
                ctx.strokeStyle = '#000';
                ctx.lineWidth = 1;
                ctx.stroke();
            }});

            // Обновляем статистику
            timeDisplay.textContent = currentStep * dt;
            stepDisplay.textContent = currentStep;
            vehiclesDisplay.textContent = Math.round(totalVehicles);
        }}

        function updateFrame(step) {{
            currentStep = step;
            slider.value = step;
            draw();
        }}

        function togglePlay() {{
            if (isPlaying) {{
                clearInterval(playInterval);
                isPlaying = false;
                playBtn.textContent = '▶ Старт';
                playBtn.classList.remove('paused');
            }} else {{
                playInterval = setInterval(() => {{
                    currentStep = (currentStep + 1) % totalSteps;
                    updateFrame(currentStep);
                }}, 100);
                isPlaying = true;
                playBtn.textContent = '⏸ Пауза';
                playBtn.classList.add('paused');
            }}
        }}

        slider.addEventListener('input', (e) => {{
            updateFrame(parseInt(e.target.value));
            if (isPlaying) togglePlay();
        }});

        // Начальная отрисовка
        draw();
    </script>
</body>
</html>'''

    return html


def main():
    parser = argparse.ArgumentParser(description="Динамическая визуализация CTM (веб-версия)")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Путь к JSON проекта")
    parser.add_argument("--output", default="traffic_viz.html", help="Выходной HTML файл")
    args = parser.parse_args()

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

    print("Запуск симуляции (это может занять минуту)...")
    states = run_simulation(network)
    print(f"Симуляция завершена. Шагов: {len(states)}")

    print("Генерация HTML визуализации...")
    html = build_visualization_html(project, network, states)

    output_path = Path(args.output)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    print(f"\n✅ Готово! Откройте файл {output_path.absolute()} в браузере.")
    print("   Там будет интерактивная анимация с ползунком времени.")

    return 0


if __name__ == "__main__":
    exit(main())