from __future__ import annotations
import math
from collections import defaultdict

from models import Project, Link
from project_loader import ProjectLoader
from project_saver import ProjectSaver

# Импортируем ядро CTM
from ctm_network_core_v2 import (
    CTMModel,
    CTMStateError,
    Incident,
    TriangularFundamentalDiagram,
)

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

        self.mass_generated = 0.0
        self.mass_entered = 0.0
        self.mass_exited = 0.0
        self.network_conservation_error_pcu = 0.0
        self.max_abs_link_conservation_error_pcu = 0.0
        self.incident_link_id = None
        self.incident_cell_index = None

        self._init_physics()
        self._build_turning_ratios()
        self._init_source_queue_history()
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
            max_wave_speed = max(diagram.free_flow_speed, diagram.backward_wave_speed)
            min_cfl_cell_length = self.dt * max_wave_speed
            cell_length_target = max(CELL_LENGTH_TARGET, min_cfl_cell_length)
            length_m = max(link.length_km * 1000.0, min_cfl_cell_length)
            cell_count = max(1, math.floor(length_m / cell_length_target))

            ctm = CTMModel.create_uniform_link(
                length=length_m,
                cell_length=length_m / cell_count,
                diagram=diagram,
                dt=self.dt,
                validate_cfl=True
            )

            # Массивы для поклеточной истории
            link.results = {
                "cell_count": cell_count,
                "history_cells_density_pcu_km": [],
                "history_flow_veh_h": [],
                "ctm_length_m": round(length_m, 3),
                "ctm_cell_length_m": round(length_m / cell_count, 3),
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

    def _init_source_queue_history(self):
        for source_id in self.sources:
            self.network.links[source_id].results["history_external_queue_pcu"] = []

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
            self.incident_cell_index = ctm.cell_count // 2
            incident = Incident(
                cell_index=self.incident_cell_index,
                start_time=300.0,
                end_time=900.0,
                capacity_factor=0.1,
                speed_factor=1.0,
            )
            ctm.incidents.append(incident)
            longest.results["incident"] = {
                "cell_index": self.incident_cell_index,
                "start_time_sec": incident.start_time,
                "end_time_sec": incident.end_time,
                "capacity_factor": incident.capacity_factor,
                "speed_factor": incident.speed_factor,
            }
            self.project.metadata["ctm_incident"] = {
                "link_id": longest.id,
                "link_name": longest.name,
                "cell_index": self.incident_cell_index,
                "start_time_sec": incident.start_time,
                "end_time_sec": incident.end_time,
                "capacity_factor": incident.capacity_factor,
                "speed_factor": incident.speed_factor,
            }

            print(f"ВНИМАНИЕ: Запланирована авария на дороге {longest.id} ({longest.name}).")
            print(f"Длина: {longest.length_km * 1000:.1f} м. Ячеек: {ctm.cell_count}.")
            print(f"Авария произойдет только в ячейке № {self.incident_cell_index} (с 5 по 15 минуту).")

    def _calc_turn_angle(self, in_link: Link, out_link: Link) -> float:
        n_start = self.network.nodes[in_link.start_node_id]
        n_mid = self.network.nodes[in_link.end_node_id]
        n_end = self.network.nodes[out_link.end_node_id]
        if None in (n_start.lon, n_start.lat, n_end.lon, n_end.lat): return 0.0
        dx1, dy1 = n_mid.lon - n_start.lon, n_mid.lat - n_start.lat
        dx2, dy2 = n_end.lon - n_mid.lon, n_end.lat - n_mid.lat
        return math.degrees(math.atan2(dx1 * dy2 - dy1 * dx2, dx1 * dx2 + dy1 * dy2))

    def step(self, t_sec: float):
        for ctm in self.ctm_links.values():
            ctm.apply_incidents()

        # 1. Опрос желаний
        demands = {l_id: ctm.demand(ctm.cells[-1]) for l_id, ctm in self.ctm_links.items()}
        supplies = {l_id: ctm.supply(ctm.cells[0]) for l_id, ctm in self.ctm_links.items()}

        actual_inflows = {l: 0.0 for l in self.ctm_links}
        actual_outflows = {l: 0.0 for l in self.ctm_links}

        # 2. Обработка краев сети
        source_rate = INFLOW_VEH_PER_HOUR / 3600.0
        for s_id in self.sources:
            ctm = self.ctm_links[s_id]
            generated = source_rate * self.dt
            ctm.external_queue += generated
            self.mass_generated += generated

            queued_demand_rate = ctm.external_queue / self.dt
            flow = min(queued_demand_rate, supplies[s_id])
            actual_inflows[s_id] = flow
            admitted = flow * self.dt
            ctm.external_queue = max(0.0, ctm.external_queue - admitted)
            self.mass_entered += admitted

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
            diagnostics = ctm.step_with_boundary_flows(
                upstream_flow=actual_inflows[link_id],
                downstream_flow=actual_outflows[link_id],
            )
            conservation_error = float(diagnostics["conservation_error_pcu"])
            self.network_conservation_error_pcu += conservation_error
            self.max_abs_link_conservation_error_pcu = max(
                self.max_abs_link_conservation_error_pcu,
                abs(conservation_error),
            )

            # 5. Сбор поклеточной истории (Снапшот раз в минуту)
            if t_sec % SNAPSHOT_INTERVAL_SEC < self.dt:
                # Массив плотностей для каждой ячейки этой дороги!
                cells_densities = [round(c.density * 1000, 1) for c in ctm.cells]
                avg_flow_veh_h = actual_outflows[link_id] * 3600.0

                link = self.project.network.links[link_id]
                link.results["history_cells_density_pcu_km"].append(cells_densities)
                link.results["history_flow_veh_h"].append(round(avg_flow_veh_h, 1))
                if link_id in self.sources:
                    link.results["history_external_queue_pcu"].append(
                        round(ctm.external_queue, 3)
                    )

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
        external_queue = sum(self.ctm_links[source_id].external_queue for source_id in self.sources)
        network_error = self.mass_entered - self.mass_exited - mass_in_network
        source_queue_error = self.mass_generated - self.mass_entered - external_queue
        demand_balance_error = (
            self.mass_generated
            - self.mass_exited
            - mass_in_network
            - external_queue
        )
        print("\n=== ДОКАЗАТЕЛЬСТВО ЗАКОНА СОХРАНЕНИЯ МАССЫ ===")
        print(f"Сгенерированный входной спрос: {self.mass_generated:.2f}")
        print(f"Машин въехало в сеть: {self.mass_entered:.2f}")
        print(f"Машин выехало: {self.mass_exited:.2f}")
        print(f"Осталось в пробках и на дорогах: {mass_in_network:.2f}")
        print(f"Осталось во внешних очередях источников: {external_queue:.2f}")
        tolerance = 1e-6 * total_steps
        self.project.metadata["ctm_simulation"] = {
            "dt_seconds": self.dt,
            "simulation_minutes": SIMULATION_MINUTES,
            "cell_length_target_m": CELL_LENGTH_TARGET,
            "strict": True,
            "validate_cfl": True,
            "total_generated_pcu": self.mass_generated,
            "total_entered_pcu": self.mass_entered,
            "total_exited_pcu": self.mass_exited,
            "mass_in_network_pcu": mass_in_network,
            "total_external_queue_pcu": external_queue,
            "conservation_error_pcu": network_error,
            "source_queue_balance_error_pcu": source_queue_error,
            "demand_balance_error_pcu": demand_balance_error,
            "sum_link_conservation_error_pcu": self.network_conservation_error_pcu,
            "max_abs_link_conservation_error_pcu": self.max_abs_link_conservation_error_pcu,
        }
        print(f"Погрешность сетевого баланса entered = exited + network: {network_error:.6f}")
        print(f"Погрешность входного баланса generated = entered + queue: {source_queue_error:.6f}")
        print(f"Погрешность полного баланса generated = exited + network + queue: {demand_balance_error:.6f}")
        print("==============================================\n")
        if (
            abs(network_error) > tolerance
            or abs(source_queue_error) > tolerance
            or abs(demand_balance_error) > tolerance
            or abs(self.network_conservation_error_pcu) > tolerance
        ):
            raise CTMStateError(
                "network conservation check failed: "
                f"network error={network_error:.9f}, "
                f"source queue error={source_queue_error:.9f}, "
                f"demand balance error={demand_balance_error:.9f}, "
                f"sum link error={self.network_conservation_error_pcu:.9f}, "
                f"tolerance={tolerance:.9f}"
            )


if __name__ == "__main__":
    input_file = "osm_network_project_map_nstu.json"
    output_file = "ctm_results_viz.json"

    project = ProjectLoader().load(input_file)
    simulator = CTMSimulator(project)
    simulator.run()

    ProjectSaver().save(project, output_file)
    print(f"Результаты сохранены в {output_file}.")
