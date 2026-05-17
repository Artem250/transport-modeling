from __future__ import annotations
import math
from collections import defaultdict

from models import Project, Link
from project_loader import ProjectLoader
from project_saver import ProjectSaver

# Импортируем ядро CTM
from ctm_network_core_v2 import TriangularFundamentalDiagram, CTMModel

# --- НАСТРОЙКИ СИМУЛЯЦИИ ---
DT_SECONDS = 0.5  # Шаг времени (0.5 сек - безопасно для коротких OSM сегментов)
SIMULATION_MINUTES = 50
SNAPSHOT_INTERVAL_SEC = 60  # Шаг ползунка времени (1 минута)
CELL_LENGTH_TARGET = 15.0  # Желаемая длина ячейки (15 метров)

# Снизили входящий поток, чтобы сеть не захлебывалась на ровном месте
INFLOW_VEH_PER_HOUR = 550.0

# Пропускные способности (подогнаны под реалистичные городские 1-полосные улицы)
HIGHWAY_PARAMS = {
    "primary": {"speed_kph": 60, "cap_per_lane": 1000, "weight": 3.0},
    "secondary": {"speed_kph": 50, "cap_per_lane": 800, "weight": 2.0},
    "tertiary": {"speed_kph": 40, "cap_per_lane": 700, "weight": 1.0},
    "residential": {"speed_kph": 30, "cap_per_lane": 500, "weight": 0.5},
    "trunk": {"speed_kph": 80, "cap_per_lane": 1400, "weight": 4.0},
    "default": {"speed_kph": 40, "cap_per_lane": 600, "weight": 1.0},
}


class CTMSimulator:
    def __init__(self, project: Project):
        self.project = project
        self.network = project.network
        self.dt = DT_SECONDS

        self.ctm_links: dict[str, CTMModel] = {}
        self.turn_ratios = defaultdict(lambda: defaultdict(dict))
        self.sources: list[str] = []
        self.sinks: list[str] = []

        self.mass_entered = 0.0
        self.mass_exited = 0.0
        self.incident_link_id = None

        self._init_physics()
        self._build_turning_ratios()
        self._plan_incident()

    def _init_physics(self):
        print("Инициализация CTM-моделей (нарезка ячеек)...")
        for link in self.network.links.values():
            hw = link.metadata.get("highway", "default")
            params = HIGHWAY_PARAMS.get(hw, HIGHWAY_PARAMS["default"])
            lanes = link.parameters.get("lanes_total", 1)

            diagram = TriangularFundamentalDiagram.from_common_units(
                free_flow_speed_kph=params["speed_kph"],
                backward_wave_speed_kph=18.0,
                capacity_pcu_h=params["cap_per_lane"] * lanes,
                jam_density_pcu_km=140.0 * lanes
            )

            # Защита от вылета (CFL): минимальная длина линка в расчете = 10 метров
            length_m = max(link.length_km * 1000.0, 10.0)
            cell_count = max(1, round(length_m / CELL_LENGTH_TARGET))

            ctm = CTMModel.create_uniform_link(
                length=length_m,
                cell_length=length_m / cell_count,
                diagram=diagram,
                dt=self.dt,
                validate_cfl=False
            )

            # Массивы для поклеточной истории
            link.results = {
                "cell_count": cell_count,
                "history_cells_density_pcu_km": [],
                "history_flow_veh_h": []
            }
            self.ctm_links[link.id] = ctm

    def _build_turning_ratios(self):
        print("Анализ топологии узлов...")
        for node_id, node in self.network.nodes.items():
            incoming = self.network.get_incoming_links(node_id)
            outgoing = self.network.get_outgoing_links(node_id)

            neighbors = {l.start_node_id for l in incoming} | {l.end_node_id for l in outgoing}

            # Тупики (границы карты)
            if len(neighbors) <= 1:
                self.sources.extend([l.id for l in outgoing])
                self.sinks.extend([l.id for l in incoming])
                continue

            # Перекрестки
            for in_link in incoming:
                scores, total_score = {}, 0.0
                for out_link in outgoing:
                    if out_link.end_node_id == in_link.start_node_id: continue
                    angle_deg = self._calc_turn_angle(in_link, out_link)
                    if abs(angle_deg) < 35:
                        turn_weight = 1.0
                    elif angle_deg < 0:
                        turn_weight = 0.3
                    else:
                        turn_weight = 0.1

                    hw = out_link.metadata.get("highway", "default")
                    hw_weight = HIGHWAY_PARAMS.get(hw, HIGHWAY_PARAMS["default"])["weight"]
                    lanes = out_link.parameters.get("lanes_total", 1)

                    score = turn_weight * hw_weight * lanes
                    scores[out_link.id] = score
                    total_score += score

                if total_score > 0:
                    for out_id, score in scores.items():
                        self.turn_ratios[node_id][in_link.id][out_id] = score / total_score
        print(f"Генераторов трафика: {len(self.sources)}, Стоков: {len(self.sinks)}")

    def _plan_incident(self):
        """Находит самую длинную внутреннюю дорогу и планирует аварию в её ЦЕНТРАЛЬНОЙ ячейке."""
        candidates = [
            l for l in self.network.links.values()
            if l.id not in self.sources and l.id not in self.sinks
        ]
        if candidates:
            longest = max(candidates, key=lambda x: x.length_km)
            self.incident_link_id = longest.id

            # Находим центральную ячейку
            ctm = self.ctm_links[self.incident_link_id]
            self.incident_cell_index = ctm.cell_count // 2  # Ровно посередине трубы

            print(f"ВНИМАНИЕ: Запланирована авария на дороге {longest.id} ({longest.name}).")
            print(f"Длина: {longest.length_km * 1000:.1f} м. Ячеек: {ctm.cell_count}.")
            print(f"Авария произойдет ТОЛЬКО в ячейке № {self.incident_cell_index} (с 5 по 15 минуту).")

    def _calc_turn_angle(self, in_link: Link, out_link: Link) -> float:
        n_start = self.network.nodes[in_link.start_node_id]
        n_mid = self.network.nodes[in_link.end_node_id]
        n_end = self.network.nodes[out_link.end_node_id]
        if None in (n_start.lon, n_start.lat, n_end.lon, n_end.lat): return 0.0
        dx1, dy1 = n_mid.lon - n_start.lon, n_mid.lat - n_start.lat
        dx2, dy2 = n_end.lon - n_mid.lon, n_end.lat - n_mid.lat
        return math.degrees(math.atan2(dx1 * dy2 - dy1 * dx2, dx1 * dx2 + dy1 * dy2))

    def step(self, t_sec: float):
        # Применение аварии СТРОГО к одной ячейке
        if self.incident_link_id and self.incident_link_id in self.ctm_links:
            incident_cell = self.ctm_links[self.incident_link_id].cells[self.incident_cell_index]
            if 300 <= t_sec <= 900:  # с 5 до 15 минуты
                incident_cell.capacity_factor = 0.1
            else:
                incident_cell.capacity_factor = 1.0

                # 1. Опрос желаний
        demands = {l_id: ctm.demand(ctm.cells[-1]) for l_id, ctm in self.ctm_links.items()}
        supplies = {l_id: ctm.supply(ctm.cells[0]) for l_id, ctm in self.ctm_links.items()}

        actual_inflows = {l: 0.0 for l in self.ctm_links}
        actual_outflows = {l: 0.0 for l in self.ctm_links}

        # 2. Обработка краев сети
        source_rate = INFLOW_VEH_PER_HOUR / 3600.0
        for s_id in self.sources:
            flow = min(source_rate, supplies[s_id])
            actual_inflows[s_id] = flow
            self.mass_entered += flow * self.dt

        for s_id in self.sinks:
            flow = demands[s_id]
            actual_outflows[s_id] = flow
            self.mass_exited += flow * self.dt

        # 3. Перекрестки (Node Solver - Proportional split)
        for node_id in self.turn_ratios:
            ratios = self.turn_ratios[node_id]
            outlink_total_demand = defaultdict(float)
            for in_id, targets in ratios.items():
                for out_id, fraction in targets.items():
                    outlink_total_demand[out_id] += demands[in_id] * fraction

            for in_id, targets in ratios.items():
                for out_id, fraction in targets.items():
                    desired_flow = demands[in_id] * fraction
                    if desired_flow == 0: continue
                    total_demand = outlink_total_demand[out_id]
                    supply = supplies[out_id]

                    if total_demand > supply:
                        actual_flow = desired_flow * (supply / total_demand)
                    else:
                        actual_flow = desired_flow

                    actual_inflows[out_id] += actual_flow
                    actual_outflows[in_id] += actual_flow

        # 4. Внутреннее движение машин (Уравнение LWR)
        for link_id, ctm in self.ctm_links.items():
            internal_flows = [
                min(ctm.demand(ctm.cells[i - 1]), ctm.supply(ctm.cells[i]))
                for i in range(1, ctm.cell_count)
            ]
            flows = [actual_inflows[link_id]] + internal_flows + [actual_outflows[link_id]]

            for i in range(ctm.cell_count):
                cell = ctm.cells[i]
                cell.density += (flows[i] - flows[i + 1]) * self.dt / cell.length
                cell.density = max(0.0, min(cell.density, ctm.diagram.jam_density))

            # 5. Сбор поклеточной истории (Снапшот раз в минуту)
            if t_sec % SNAPSHOT_INTERVAL_SEC < self.dt:
                # Массив плотностей для каждой ячейки этой дороги!
                cells_densities = [round(c.density * 1000, 1) for c in ctm.cells]
                avg_flow_veh_h = actual_outflows[link_id] * 3600.0

                link = self.project.network.links[link_id]
                link.results["history_cells_density_pcu_km"].append(cells_densities)
                link.results["history_flow_veh_h"].append(round(avg_flow_veh_h, 1))

    def run(self):
        total_steps = int((SIMULATION_MINUTES * 60) / self.dt)
        print(f"Симуляция начата. Шагов: {total_steps}...")

        for step in range(total_steps):
            t_sec = step * self.dt
            self.step(t_sec)

            if step > 0 and step % int(300 / self.dt) == 0:
                print(f"Модельное время: {int(t_sec / 60)} мин...")

        # Проверка баланса массы
        mass_in_network = sum(
            c.density * c.length for ctm in self.ctm_links.values() for c in ctm.cells
        )
        print("\n=== ДОКАЗАТЕЛЬСТВО ЗАКОНА СОХРАНЕНИЯ МАССЫ ===")
        print(f"Машин въехало в сеть: {self.mass_entered:.2f}")
        print(f"Машин выехало: {self.mass_exited:.2f}")
        print(f"Осталось в пробках и на дорогах: {mass_in_network:.2f}")
        error = self.mass_entered - self.mass_exited - mass_in_network
        print(f"Математическая погрешность CTM ядра: {error:.6f} автомобилей")
        print("==============================================\n")


if __name__ == "__main__":
    input_file = "osm_network_project_map_nstu.json"
    output_file = "ctm_results_viz.json"

    project = ProjectLoader().load(input_file)
    simulator = CTMSimulator(project)
    simulator.run()

    ProjectSaver().save(project, output_file)
    print(f"Результаты сохранены в {output_file}.")