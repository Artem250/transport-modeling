"""Archived pre-core network CTM prototype.

The active runner imports ``ctm_network_simulator``. This file is retained only
for historical comparison with the old manual density update path.
"""

from __future__ import annotations

import math
from collections import defaultdict

from models import Project, Link
from project_loader import ProjectLoader
from project_saver import ProjectSaver

# Импортируем ядро. Убедитесь, что файл называется ctm_network_core_v2.py
from ctm_network_core_v2 import TriangularFundamentalDiagram, CTMModel

# --- НАСТРОЙКИ СИМУЛЯЦИИ ---
# Используем малый шаг (0.2 сек), так как в OSM есть короткие линки по 5 метров.
DT_SECONDS = 0.2
SIMULATION_MINUTES = 15
CELL_LENGTH_TARGET = 10.0  # Нарезаем длинные дороги на куски по 10 метров
INFLOW_VEH_PER_HOUR = 900.0  # Трафик на въездах в сеть

# Физические параметры по типам дорог OSM
HIGHWAY_PARAMS = {
    "primary": {"speed_kph": 60, "cap_per_lane": 900, "weight": 3.0},
    "secondary": {"speed_kph": 50, "cap_per_lane": 700, "weight": 2.0},
    "tertiary": {"speed_kph": 40, "cap_per_lane": 600, "weight": 1.0},
    "residential": {"speed_kph": 30, "cap_per_lane": 300, "weight": 0.2},
    "trunk": {"speed_kph": 80, "cap_per_lane": 1200, "weight": 4.0},
    "default": {"speed_kph": 40, "cap_per_lane": 600, "weight": 1.0},
}


class CTMSimulator:
    def __init__(self, project: Project):
        self.project = project
        self.network = project.network
        self.dt = DT_SECONDS

        self.ctm_links: dict[str, CTMModel] = {}
        self.turn_ratios: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))

        self.sources: list[str] = []
        self.sinks: list[str] = []

        self._init_physics()
        self._build_turning_ratios()

    def _init_physics(self):
        print("Инициализация CTM-моделей для дорог...")
        for link in self.network.links.values():
            highway = link.metadata.get("highway", "default")
            if highway not in HIGHWAY_PARAMS:
                highway = "default"

            params = HIGHWAY_PARAMS[highway]
            lanes = link.parameters.get("lanes_total", 1)

            diagram = TriangularFundamentalDiagram.from_common_units(
                free_flow_speed_kph=params["speed_kph"],
                backward_wave_speed_kph=18.0,
                capacity_pcu_h=params["cap_per_lane"] * lanes,
                jam_density_pcu_km=140.0 * lanes
            )

            length_m = max(link.length_km * 1000.0, 5.0)
            cell_count = max(1, round(length_m / CELL_LENGTH_TARGET))

            ctm = CTMModel.create_uniform_link(
                length=length_m,
                cell_length=length_m / cell_count,
                diagram=diagram,
                dt=self.dt,
                validate_cfl=False
            )
            self.ctm_links[link.id] = ctm

    def _build_turning_ratios(self):
        print("Анализ перекрестков и расчет поворотных коэффициентов...")
        for node_id, node in self.network.nodes.items():
            incoming = self.network.get_incoming_links(node_id)
            outgoing = self.network.get_outgoing_links(node_id)

            # --- ИСПРАВЛЕННАЯ ЛОГИКА ГРАНИЦ ---
            # Собираем всех уникальных соседей этого узла
            neighbors = set()
            for l in incoming: neighbors.add(l.start_node_id)
            for l in outgoing: neighbors.add(l.end_node_id)

            # Если сосед всего 1, значит это тупик (обрубленный край карты OSM)
            if len(neighbors) <= 1:
                # Всё, что выходит из тупика в город - это Источник трафика
                self.sources.extend([l.id for l in outgoing])
                # Всё, что возвращается из города в тупик - это Сток (удаляем машины)
                self.sinks.extend([l.id for l in incoming])
                continue  # Перекрестка тут нет, поворачивать некуда
            # -----------------------------------

            # Внутренние перекрестки (где соседей 2 и больше)
            for in_link in incoming:
                scores = {}
                total_score = 0.0
                for out_link in outgoing:
                    # Запрет разворота в тот же линк (U-turn)
                    if out_link.end_node_id == in_link.start_node_id:
                        continue

                    angle_deg = self._calc_turn_angle(in_link, out_link)

                    if abs(angle_deg) < 35:
                        turn_weight = 1.0  # Прямо
                    elif angle_deg < 0:
                        turn_weight = 0.3  # Направо
                    else:
                        turn_weight = 0.1  # Налево

                    hw = out_link.metadata.get("highway", "default")
                    hw_weight = HIGHWAY_PARAMS.get(hw, HIGHWAY_PARAMS["default"])["weight"]
                    lanes = out_link.parameters.get("lanes_total", 1)

                    score = turn_weight * hw_weight * lanes
                    scores[out_link.id] = score
                    total_score += score

                # Нормализация в проценты
                if total_score > 0:
                    for out_id, score in scores.items():
                        self.turn_ratios[node_id][in_link.id][out_id] = score / total_score

        print(f"Найдено источников: {len(self.sources)}, стоков: {len(self.sinks)}")

    def _calc_turn_angle(self, in_link: Link, out_link: Link) -> float:
        n_start = self.network.nodes[in_link.start_node_id]
        n_mid = self.network.nodes[in_link.end_node_id]
        n_end = self.network.nodes[out_link.end_node_id]

        if None in (n_start.lon, n_start.lat, n_end.lon, n_end.lat):
            return 0.0

        dx1, dy1 = n_mid.lon - n_start.lon, n_mid.lat - n_start.lat
        dx2, dy2 = n_end.lon - n_mid.lon, n_end.lat - n_mid.lat
        return math.degrees(math.atan2(dx1 * dy2 - dy1 * dx2, dx1 * dx2 + dy1 * dy2))

    def step(self):
        demands = {l_id: ctm.demand(ctm.cells[-1]) for l_id, ctm in self.ctm_links.items()}
        supplies = {l_id: ctm.supply(ctm.cells[0]) for l_id, ctm in self.ctm_links.items()}

        actual_inflows = {l: 0.0 for l in self.ctm_links}
        actual_outflows = {l: 0.0 for l in self.ctm_links}

        # 1. Генерация внешнего трафика
        source_rate = INFLOW_VEH_PER_HOUR / 3600.0  # машин в секунду
        for s_id in self.sources:
            actual_inflows[s_id] = min(source_rate, supplies[s_id])

        for s_id in self.sinks:
            actual_outflows[s_id] = demands[s_id]

        # 2. Решатель узлов
        for node_id in self.turn_ratios:
            ratios = self.turn_ratios[node_id]

            # Подсчет спроса на исходящие линки
            outlink_total_demand = defaultdict(float)
            for in_id, targets in ratios.items():
                for out_id, fraction in targets.items():
                    outlink_total_demand[out_id] += demands[in_id] * fraction

            # Удовлетворение спроса (с учетом пробок)
            for in_id, targets in ratios.items():
                for out_id, fraction in targets.items():
                    desired_flow = demands[in_id] * fraction
                    if desired_flow == 0:
                        continue

                    total_demand = outlink_total_demand[out_id]
                    supply = supplies[out_id]

                    if total_demand > supply:
                        actual_flow = desired_flow * (supply / total_demand)
                    else:
                        actual_flow = desired_flow

                    actual_inflows[out_id] += actual_flow
                    actual_outflows[in_id] += actual_flow

        # 3. Обновление плотностей в ячейках
        for link_id, ctm in self.ctm_links.items():
            internal_flows = [
                min(ctm.demand(ctm.cells[i - 1]), ctm.supply(ctm.cells[i]))
                for i in range(1, ctm.cell_count)
            ]
            flows = [actual_inflows[link_id]] + internal_flows + [actual_outflows[link_id]]

            for i in range(ctm.cell_count):
                cell = ctm.cells[i]
                # Изменение плотности: (Приток - Отток) * dt / Длину ячейки
                cell.density += (flows[i] - flows[i + 1]) * self.dt / cell.length
                # Защита от математических погрешностей float
                cell.density = max(0.0, min(cell.density, ctm.diagram.jam_density))

    def run(self, duration_sec: int):
        print(f"Запуск симуляции на {duration_sec} секунд (модельного времени)...")
        steps = int(duration_sec / self.dt)
        for t in range(steps):
            self.step()
            if t > 0 and t % int(300 / self.dt) == 0:
                print(f"Прошло {round(t * self.dt)} секунд...")
        self._export_results()

    def _export_results(self):
        print("Сбор результатов и расчет LOS...")
        for link_id, ctm in self.ctm_links.items():
            avg_density = sum(c.density for c in ctm.cells) / ctm.cell_count
            crit_density = ctm.diagram.critical_density

            vc_ratio = avg_density / crit_density if crit_density > 0 else 0

            if vc_ratio < 0.3:
                los = "A"
            elif vc_ratio < 0.5:
                los = "B"
            elif vc_ratio < 0.7:
                los = "C"
            elif vc_ratio < 0.9:
                los = "D"
            elif vc_ratio <= 1.0:
                los = "E"
            else:
                los = "F"

            self.project.network.links[link_id].results = {
                "Density_pcu_km": round(avg_density * 1000, 1),
                "VC_ratio": round(vc_ratio, 3),
                "LOS": los
            }


if __name__ == "__main__":
    input_file = "osm_network_project_map_nstu.json"
    output_file = "ctm_results_viz.json"

    loader = ProjectLoader()
    try:
        project = loader.load(input_file)
    except FileNotFoundError:
        print(f"Ошибка: файл {input_file} не найден!")
        exit(1)

    simulator = CTMSimulator(project)

    # Гоняем симуляцию 15 минут (900 секунд)
    simulator.run(duration_sec=900)

    ProjectSaver().save(project, output_file)
    print(f"\nГотово! Результаты сохранены в {output_file}.")
    print("Откройте этот файл в traffic_viz.py, чтобы увидеть раскрашенную сеть.")
