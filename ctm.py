from __future__ import annotations

from collections import defaultdict
from typing import Dict, Any, Optional

from models import Movement, SimulationConfig
from network_dynamic import DynamicNetwork


class CTMSimulator:
    def __init__(self, network: DynamicNetwork, config: SimulationConfig):
        self.network = network
        self.config = config

        # Определяем шаг времени
        first_link = next(iter(network.links.values()), None)
        if first_link is not None:
            # Проверяем, словарь ли это или объект
            if isinstance(first_link, dict):
                self.dt_seconds = first_link.get('dt_seconds', max(config.min_dt_seconds,
                                                                   min(config.dt_seconds, config.max_dt_seconds)))
            else:
                self.dt_seconds = getattr(first_link, 'dt_seconds',
                                          max(config.min_dt_seconds, min(config.dt_seconds, config.max_dt_seconds)))
        else:
            self.dt_seconds = max(config.min_dt_seconds, min(config.dt_seconds, config.max_dt_seconds))

        # Важно: создаем атрибут time_step_s, который используется в расчетах
        self.time_step_s = self.dt_seconds

    def simulate(self, horizon_seconds: int | None = None) -> dict[str, dict]:
        horizon = horizon_seconds or self.config.horizon_seconds
        steps = max(int(horizon / self.dt_seconds), 1)

        stats = {
            link_id: {
                "occupancy_sum": 0.0,
                "max_queue_pcu": 0.0,
                "outflow_total": 0.0,
                "inflow_total": 0.0,
                "peak_flow_step": 0.0,
            }
            for link_id in self.network.links
        }

        for step in range(steps):
            current_time_s = step * self.dt_seconds

            # Вычисляем потоки
            internal_flows = self._compute_internal_flows()
            node_flows = self._compute_node_flows(current_time_s)
            sink_flows = self._compute_sink_flows()
            source_flows = self._compute_source_flows(current_time_s)

            for link_id, link in self.network.links.items():
                inflows = [0.0 for _ in
                           range(len(link.cells) if hasattr(link, 'cells') else len(link.get('cells', [])))]
                outflows = [0.0 for _ in
                            range(len(link.cells) if hasattr(link, 'cells') else len(link.get('cells', [])))]

                # Внутренние потоки между ячейками
                for cell_index, flow in internal_flows.get(link_id, {}).items():
                    if cell_index < len(outflows):
                        outflows[cell_index] += flow
                    if cell_index + 1 < len(inflows):
                        inflows[cell_index + 1] += flow

                # Потоки через узлы (повороты)
                # Суммируем исходящие потоки с этого линка
                node_out = 0.0
                node_in = 0.0

                for movement_id, flow in node_flows.items():
                    mov = self.network.movements.get(movement_id)
                    if not mov:
                        continue

                    # Безопасное получение from_link_id
                    if isinstance(mov, dict):
                        m_from = mov.get('from_link_id')
                        m_to = mov.get('to_link_id')
                    else:
                        m_from = getattr(mov, 'from_link_id', None)
                        m_to = getattr(mov, 'to_link_id', None)

                    if m_from == link_id:
                        node_out += flow
                    if m_to == link_id:
                        node_in += flow

                # Обновляем состояние ячеек
                cells = link.cells if hasattr(link, 'cells') else link.get('cells', [])
                if cells:
                    # Последняя ячейка отдает во внешний мир (sink) и на повороты
                    outflows[-1] += node_out + sink_flows.get(link_id, 0.0)
                    # Первая ячейка принимает из внешнего мира (source) и с поворотов
                    inflows[0] += node_in + source_flows.get(link_id, 0.0)

                for cell_index, cell in enumerate(cells):
                    # Обновляем занятость: old + inflow - outflow
                    current_occ = cell.occupancy_pcu if hasattr(cell, 'occupancy_pcu') else cell.get('occupancy_pcu',
                                                                                                     0.0)
                    new_occ = max(current_occ + inflows[cell_index] - outflows[cell_index], 0.0)

                    if hasattr(cell, 'occupancy_pcu'):
                        cell.occupancy_pcu = new_occ
                    else:
                        cell['occupancy_pcu'] = new_occ

                total_outflow = node_out + sink_flows.get(link_id, 0.0)
                total_inflow = node_in + source_flows.get(link_id, 0.0)

                # Считаем общую занятость линка
                if hasattr(link, 'total_occupancy'):
                    total_occupancy = link.total_occupancy()
                else:
                    # Для словарей считаем вручную
                    total_occupancy = sum(c.get('occupancy_pcu', 0.0) for c in link.get('cells', []))

                stats[link_id]["outflow_total"] += total_outflow
                stats[link_id]["inflow_total"] += total_inflow
                stats[link_id]["occupancy_sum"] += total_occupancy
                stats[link_id]["max_queue_pcu"] = max(stats[link_id]["max_queue_pcu"], total_occupancy)
                stats[link_id]["peak_flow_step"] = max(stats[link_id]["peak_flow_step"], total_outflow)

        # Пост-обработка статистики
        for link_id, values in stats.items():
            link_obj = self.network.links[link_id]
            length_km = getattr(link_obj, 'length_km', None) or (
                link_obj.get('length_km', 0.001) if isinstance(link_obj, dict) else 0.001)
            length_km = max(length_km, 0.001)

            values["avg_density_pcu_km"] = values["occupancy_sum"] / steps / length_km
            values["avg_flow_pcu_h"] = values["outflow_total"] * 3600.0 / max(horizon, 1)
            values["peak_flow_pcu_h"] = values["peak_flow_step"] * 3600.0 / self.dt_seconds
            values["throughput_pcu"] = values["outflow_total"]

        return stats

    def step(self, current_time_s: float):
        internal_flows = self._compute_internal_flows()
        node_flows = self._compute_node_flows(current_time_s)
        sink_flows = self._compute_sink_flows()
        source_flows = self._compute_source_flows(current_time_s)

        for link_id, link in self.network.links.items():
            inflows = [0.0 for _ in
                       range(len(link.cells) if hasattr(link, 'cells') else len(link.get('cells', [])))]
            outflows = [0.0 for _ in
                        range(len(link.cells) if hasattr(link, 'cells') else len(link.get('cells', [])))]

            # Внутренние потоки между ячейками
            for cell_index, flow in internal_flows.get(link_id, {}).items():
                if cell_index < len(outflows):
                    outflows[cell_index] += flow
                if cell_index + 1 < len(inflows):
                    inflows[cell_index + 1] += flow

            # Потоки через узлы (повороты)
            # Суммируем исходящие потоки с этого линка
            node_out = 0.0
            node_in = 0.0

            for movement_id, flow in node_flows.items():
                mov = self.network.movements.get(movement_id)
                if not mov:
                    continue

                # Безопасное получение from_link_id
                if isinstance(mov, dict):
                    m_from = mov.get('from_link_id')
                    m_to = mov.get('to_link_id')
                else:
                    m_from = getattr(mov, 'from_link_id', None)
                    m_to = getattr(mov, 'to_link_id', None)

                if m_from == link_id:
                    node_out += flow
                if m_to == link_id:
                    node_in += flow

            # Обновляем состояние ячеек
            cells = link.cells if hasattr(link, 'cells') else link.get('cells', [])
            if cells:
                # Последняя ячейка отдает во внешний мир (sink) и на повороты
                outflows[-1] += node_out + sink_flows.get(link_id, 0.0)
                # Первая ячейка принимает из внешнего мира (source) и с поворотов
                inflows[0] += node_in + source_flows.get(link_id, 0.0)

            for cell_index, cell in enumerate(cells):
                # Обновляем занятость: old + inflow - outflow
                current_occ = cell.occupancy_pcu if hasattr(cell, 'occupancy_pcu') else cell.get('occupancy_pcu',
                                                                                                 0.0)
                new_occ = max(current_occ + inflows[cell_index] - outflows[cell_index], 0.0)

                if hasattr(cell, 'occupancy_pcu'):
                    cell.occupancy_pcu = new_occ
                else:
                    cell['occupancy_pcu'] = new_occ

            total_outflow = node_out + sink_flows.get(link_id, 0.0)
            total_inflow = node_in + source_flows.get(link_id, 0.0)

            # Считаем общую занятость линка
            if hasattr(link, 'total_occupancy'):
                total_occupancy = link.total_occupancy()
            else:
                # Для словарей считаем вручную
                total_occupancy = sum(c.get('occupancy_pcu', 0.0) for c in link.get('cells', []))

            stats[link_id]["outflow_total"] += total_outflow
            stats[link_id]["inflow_total"] += total_inflow
            stats[link_id]["occupancy_sum"] += total_occupancy
            stats[link_id]["max_queue_pcu"] = max(stats[link_id]["max_queue_pcu"], total_occupancy)
            stats[link_id]["peak_flow_step"] = max(stats[link_id]["peak_flow_step"], total_outflow)

    def _compute_internal_flows(self) -> dict[str, dict[int, float]]:
        flows: dict[str, dict[int, float]] = defaultdict(dict)
        for link_id, link in self.network.links.items():
            cells = link.cells if hasattr(link, 'cells') else link.get('cells', [])
            cell_count = len(cells)

            if cell_count <= 1:
                continue

            for cell_index in range(cell_count - 1):
                cell_curr = cells[cell_index]
                cell_next = cells[cell_index + 1]

                occ_curr = cell_curr.occupancy_pcu if hasattr(cell_curr, 'occupancy_pcu') else cell_curr.get(
                    'occupancy_pcu', 0.0)

                # Отправляющая способность текущей ячейки
                sending = link.sending_capacity(cell_index) if hasattr(link, 'sending_capacity') else link.get(
                    'sending_capacity', lambda x: 0)(cell_index)

                # Принимающая способность следующей ячейки
                receiving = link.receiving_capacity(cell_index + 1) if hasattr(link,
                                                                               'receiving_capacity') else link.get(
                    'receiving_capacity', lambda x: 0)(cell_index + 1)

                flows[link_id][cell_index] = min(sending, receiving)

        return flows

    def _compute_source_flows(self, current_time_s: float) -> Dict[str, float]:
        source_flows = {}
        for source_id, source in self.network.sources.items():
            # Поддержка и словарей, и объектов
            if isinstance(source, dict):
                start_time = source.get('start_time_s', 0)
                end_time = source.get('end_time_s', float('inf'))
                flow_rate = source.get('flow_rate_pcu_h', 0)
            else:
                start_time = getattr(source, 'start_time_s', 0)
                end_time = getattr(source, 'end_time_s', float('inf'))
                flow_rate = getattr(source, 'flow_rate_pcu_h', 0)

            if current_time_s < start_time:
                continue
            if current_time_s >= end_time:
                continue

            # Переводим поток из pcu/h в pcu/step
            source_flows[source_id] = (flow_rate / 3600.0) * self.time_step_s
        return source_flows

    def _compute_sink_flows(self) -> Dict[str, float]:
        """Вычисляет максимально возможный сброс транспорта в стоки."""
        sink_flows = {}
        for sink_id, sink in self.network.sinks.items():
            link_id = None
            if isinstance(sink, dict):
                link_id = sink.get('link_id')
            else:
                link_id = getattr(sink, 'link_id', None)

            if not link_id:
                continue

            link = self.network.links.get(link_id)
            if not link:
                continue

            # Пропускная способность линка (максимум что может уйти в сток)
            if isinstance(link, dict):
                capacity = link.get('capacity_pcu_h', float('inf'))
            else:
                capacity = getattr(link, 'capacity_pcu_h', float('inf'))

            # Ограничиваем поток пропускной способностью
            sink_flows[sink_id] = (capacity / 3600.0) * self.time_step_s if capacity != float(
                'inf') else 1000.0  # Заглушка для бесконечности

        return sink_flows

    def _compute_node_flows(self, current_time_s: float) -> Dict[str, float]:
        """Вычисляет потоки через узлы (повороты) с учетом Sending/Receiving принципов CTM."""
        node_flows = {}

        # 1. Считаем доступный объем для отправки с каждого участка (S_i)
        # S_i = min(n_i, Q_i * dt)
        sending_capacity = {}
        for link_id, link in self.network.links.items():
            cells = link.cells if hasattr(link, 'cells') else link.get('cells', [])
            if not cells:
                sending_capacity[link_id] = 0.0
                continue

            # Суммарное количество машин на участке
            n_i = sum(
                (c.occupancy_pcu if hasattr(c, 'occupancy_pcu') else c.get('occupancy_pcu', 0.0))
                for c in cells
            )

            # Максимальный поток за шаг (Q * dt)
            if isinstance(link, dict):
                capacity_flow = link.get('capacity_pcu_h', 0.0)
            else:
                capacity_flow = getattr(link, 'capacity_pcu_h', 0.0)

            q_max_step = (capacity_flow / 3600.0) * self.time_step_s
            sending_capacity[link_id] = min(n_i, q_max_step)

        # 2. Считаем доступное место для приема на каждом участке (R_j)
        # R_j = min(Q_j, w * (k_jam - k_j)) * dt
        receiving_capacity = {}
        for link_id, link in self.network.links.items():
            cells = link.cells if hasattr(link, 'cells') else link.get('cells', [])
            if not cells:
                receiving_capacity[link_id] = 0.0
                continue

            # Средняя плотность
            if isinstance(link, dict):
                length_m = link.get('length_m', 1.0)
                jam_density = link.get('jam_density', 0.15)
                capacity_flow = link.get('capacity_pcu_h', 0.0)
                wave_speed = link.get('wave_speed_m_s', 5.0)
            else:
                length_m = getattr(link, 'length_m', 1.0)
                jam_density = getattr(link, 'jam_density', 0.15)
                capacity_flow = getattr(link, 'capacity_pcu_h', 0.0)
                wave_speed = getattr(link, 'wave_speed_m_s', 5.0)

            total_occ = sum(
                (c.occupancy_pcu if hasattr(c, 'occupancy_pcu') else c.get('occupancy_pcu', 0.0))
                for c in cells
            )
            density = total_occ / length_m if length_m > 0 else 0.0

            # Формула принимающей способности
            flow_by_wave = wave_speed * (jam_density - density) * length_m
            q_recv_step = (capacity_flow / 3600.0) * self.time_step_s

            receiving_capacity[link_id] = max(0.0, min(q_recv_step, flow_by_wave * self.time_step_s))

        # 3. Распределяем потоки через повороты (Movements)
        for movement_id, movement in self.network.movements.items():
            # Безопасное получение атрибутов
            if isinstance(movement, dict):
                from_link_id = movement.get('from_link_id')
                to_link_id = movement.get('to_link_id')
                split_ratio = movement.get('split_ratio', 1.0)
                control = movement.get('control', {})
            else:
                from_link_id = getattr(movement, 'from_link_id', None)
                to_link_id = getattr(movement, 'to_link_id', None)
                split_ratio = getattr(movement, 'split_ratio', 1.0)
                control = getattr(movement, 'control', {})

            if from_link_id is None or to_link_id is None:
                continue

            from_available = sending_capacity.get(from_link_id, 0.0)
            to_available = receiving_capacity.get(to_link_id, 0.0)

            # Учет светофоров
            signal_factor = 1.0
            if control:
                ctrl_type = control.get('control_type') if isinstance(control, dict) else getattr(control,
                                                                                                  'control_type', None)
                if ctrl_type == 'signal':
                    # Упрощенная логика: если есть фазы, проверяем время
                    # В полной версии здесь нужен расчет фазы по current_time_s
                    # Пока считаем, что светофор пропускает часть потока (green_ratio) или 0
                    green_ratio = control.get('green_ratio', 0.5) if isinstance(control, dict) else getattr(control,
                                                                                                            'green_ratio',
                                                                                                            0.5)

                    # Если есть точное расписание фаз, можно сделать точнее,
                    # но для демонстрации используем средний коэффициент
                    signal_factor = green_ratio

                    # Поток = min(Сколько хотим отправить * доля, Сколько могут принять) * сигнал
            potential_flow = from_available * split_ratio
            actual_flow = min(potential_flow, to_available) * signal_factor

            node_flows[movement_id] = actual_flow

        return node_flows

    def _movement_capacity_step(self, movement: Any, current_time_s: float) -> float:
        """Вспомогательный метод для расчета пропускной способности конкретного маневра."""
        # Получаем from_link
        if isinstance(movement, dict):
            from_link_id = movement.get('from_link_id')
            cap_pcu_h = movement.get('capacity_pcu_h')
            ctrl = movement.get('control', {})
            mov_id = movement.get('id')
        else:
            from_link_id = getattr(movement, 'from_link_id', None)
            cap_pcu_h = getattr(movement, 'capacity_pcu_h', None)
            ctrl = getattr(movement, 'control', {})
            mov_id = getattr(movement, 'id', None)

        if not from_link_id or from_link_id not in self.network.links:
            return 0.0

        from_link = self.network.links[from_link_id]

        # Базовая пропускная способность
        base_cap = cap_pcu_h
        if base_cap is None:
            if isinstance(from_link, dict):
                base_cap = from_link.get('capacity_pcu_h', 0.0)
            else:
                base_cap = getattr(from_link, 'capacity_pcu_h', 0.0)

        multiplier = 1.0
        if ctrl:
            ctrl_type = ctrl.get('control_type') if isinstance(ctrl, dict) else getattr(ctrl, 'control_type',
                                                                                        'uncontrolled')

            if ctrl_type == 'signal':
                cycle_time = ctrl.get('cycle_time_s', 0) if isinstance(ctrl, dict) else getattr(ctrl, 'cycle_time_s', 0)
                phases = ctrl.get('phases', []) if isinstance(ctrl, dict) else getattr(ctrl, 'phases', [])

                if cycle_time > 0 and phases:
                    cycle_offset = current_time_s % cycle_time
                    phase_open = False
                    for phase in phases:
                        allowed = phase.get('green_for_movements', []) if isinstance(phase, dict) else getattr(phase,
                                                                                                               'green_for_movements',
                                                                                                               [])
                        start_s = phase.get('start_s', 0) if isinstance(phase, dict) else getattr(phase, 'start_s', 0)
                        end_s = phase.get('end_s', 0) if isinstance(phase, dict) else getattr(phase, 'end_s', 0)

                        if mov_id and mov_id not in allowed:
                            continue
                        if start_s <= cycle_offset < end_s:
                            phase_open = True
                            break
                    multiplier = 1.0 if phase_open else 0.0
                else:
                    gr = ctrl.get('green_ratio', 0.5) if isinstance(ctrl, dict) else getattr(ctrl, 'green_ratio', 0.5)
                    multiplier = gr if gr else 0.5
            elif ctrl_type == 'roundabout':
                multiplier = 0.9
            elif ctrl_type == 'priority':
                multiplier = 0.85

        return max(base_cap * multiplier * self.dt_seconds / 3600.0, 0.0)