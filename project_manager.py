import json
import os
# Импорты остались те же
from road_sections import StraightRoad, Intersection, TARGET_LOS_VC, BASE_SPEED_KPH
from corridor import CorridorRoute
from report_formatter import ReportFormatter


class TrafficProject:
    """
    Менеджер, управляющий загрузкой данных, ссылками, маршрутами и запуском анализа.
    Основан на логике project_manager_1.py.
    """

    def __init__(self, json_file_path, report_path_txt="analysis_report.txt"):
        self.json_path = json_file_path
        self.report_path = report_path_txt
        self.links = {}
        self.routes = []
        self.pcu_coeffs = {}
        self.project_name = ""

    def load_data(self):
        """Загружает данные из JSON-файла (как в pm1)."""
        if not os.path.exists(self.json_path):
            print(f"Ошибка: Файл '{self.json_path}' не найден!")
            return False

        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            self.project_name = data.get('project_name', 'Unknown Project')
            self.pcu_coeffs = data.get('pcu_coefficients', {})

            # Создание ссылок
            for item in data['directional_links']:
                link_type = item['type']
                link_id = item['id']

                # Инициализация объектов (без изменений, как в pm1)
                if link_type == 'straight':
                    section = StraightRoad(
                        item['id'], item['name'], item['traffic_counts'], self.pcu_coeffs,
                        item['length_km'], item.get('lanes_total', 1), item.get('lanes_bus', 0),
                        item.get('capacity_per_lane_base', 1800), item.get('lane_width_m', 3.5),
                        item.get('grade_percent', 0.0), item.get('parking_present', False),
                        item.get('heavy_vehicles_percent', 0.0)
                    )
                elif link_type == 'intersection':
                    section = Intersection(
                        item['id'], item['name'], item['traffic_counts'], self.pcu_coeffs,
                        item['length_km'], item.get('cycle_time', 100), item.get('green_time', 30),
                        item.get('saturation_flow_base', 1800), item.get('lanes_count', 1),
                        item.get('lane_width_m', 3.5), item.get('grade_percent', 0.0),
                        item.get('parking_present', False), item.get('heavy_vehicles_percent', 0.0)
                    )
                    if 'g_others' in item:
                        section.g_others = item['g_others']

                # Важно: сохраняем тип, чтобы viz знал, кто есть кто
                section.analysis_data['type'] = link_type
                self.links[link_id] = section

            # Создание маршрутов
            for route_data in data.get('routes', []):
                route_links = []
                for lid in route_data['links']:
                    if lid in self.links:
                        route_links.append(self.links[lid])

                if route_links:
                    route = CorridorRoute(route_data['id'], route_data['name'], route_links)
                    self.routes.append(route)

            print(f"Загружено: {len(self.links)} ссылок, {len(self.routes)} маршрутов.")
            return True

        except Exception as e:
            print(f"Ошибка при загрузке данных: {e}")
            return False

    def run_full_analysis(self):
        links_report = []

        for link in self.links.values():
            # ОШИБКА БЫЛА ТУТ: вызываем analyze_performance, который
            # делает ВСЁ: Capacity -> VC -> LOS -> Delay -> Запись в словарь
            link.analyze_performance()

            # Оптимизация
            optimization_result = link.optimize()

            # Сбор данных для отчета
            link_data = link.get_report_data()

            if optimization_result:
                opt_data = {
                    'Optimization_Proposal': optimization_result['proposal'],
                    'C_optimized': round(optimization_result['C_new'], 0),
                    'VC_optimized': round(optimization_result['vc_new'], 3),
                    'LOS_optimized': optimization_result['los_new']
                }
                link_data.update(opt_data)
                link.analysis_data.update(opt_data)

            links_report.append(link_data)

        # Расчет маршрутов (теперь Delay_sec точно есть)
        routes_report = []
        for route in self.routes:
            route.calculate_kpi()
            routes_report.append(route.get_report_data())

        return {
            'Project_Name': self.project_name,
            'Links_Analysis': links_report,
            'Routes_Analysis': routes_report
        }

    def export_report(self, report_data):
        """Сохраняет текстовый отчет (как в pm1)."""
        formatter = ReportFormatter(self.project_name, report_data)
        report_content = formatter.generate_report()

        try:
            with open(self.report_path, 'w', encoding='utf-8') as f:
                f.write(report_content)
            print(f"Текстовый отчет сохранен в '{self.report_path}'")
        except Exception as e:
            print(f"Ошибка записи отчета: {e}")

    def export_json_for_viz(self, output_path="viz_data.json"):
        """
        НОВАЯ ФУНКЦИЯ. Сохраняет данные для traffic_viz.py.
        Не влияет на расчеты, просто собирает results + coords.
        """
        # Дефолтные координаты (на случай отсутствия файла)
        DEFAULT_MAP = {
            "L5_RING_ENTRY": [82.888, 55.050, 82.890, 55.050],
            "L_RING_CIRCULATION": [82.890, 55.050, 82.892, 55.052],
            "L1_RING_EXIT": [82.892, 55.052, 82.894, 55.054],
            "L2_A_RING_TO_PED": [82.893, 55.050, 82.897, 55.050],
            "L2_PED_SIGNAL": [82.897, 55.050, 82.898, 55.050],
            "L2_B_PED_TO_I3": [82.898, 55.050, 82.902, 55.050],
            "L3_I3_APPROACH": [82.902, 55.050, 82.904, 55.050],
            "L4_I3_EXIT": [82.904, 55.049, 82.902, 55.049],
            "L6_B_I3_TO_PED": [82.902, 55.049, 82.898, 55.049],
            "L6_PED_SIGNAL": [82.898, 55.049, 82.897, 55.049],
            "L6_A_PED_TO_RING": [82.897, 55.049, 82.891, 55.049]
        }

        # Пытаемся подгрузить сохраненные позиции
        coords_source = DEFAULT_MAP.copy()
        if os.path.exists("saved_positions.json"):
            try:
                with open("saved_positions.json", 'r', encoding='utf-8') as f:
                    coords_source.update(json.load(f))
            except:
                pass

        links_data_json = []
        for link_id, link in self.links.items():
            # Берем те самые данные, которые посчитались в run_full_analysis
            res_data = link.analysis_data

            # Координаты
            raw = coords_source.get(link_id, [0, 0, 0, 0])
            c_dict = raw if isinstance(raw, dict) else {
                "lon_start": raw[0], "lat_start": raw[1],
                "lon_end": raw[2], "lat_end": raw[3]
            }

            links_data_json.append({
                "id": link_id,
                "name": link.name,
                "results": res_data,
                "coords": c_dict
            })

        # Данные маршрутов для визуала (хоть сейчас и не рисуются)
        routes_json = [r.get_report_data() for r in self.routes]

        full_json = {
            "project_name": self.project_name,
            "links": links_data_json,
            "routes": routes_json
        }

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(full_json, f, indent=2, ensure_ascii=False)
            print(f"JSON для визуализации обновлен: {output_path}")
        except Exception as e:
            print(f"Ошибка сохранения JSON: {e}")