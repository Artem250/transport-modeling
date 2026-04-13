import json
import os

from analysis_service import AnalysisService
from project_loader import ProjectLoader
from project_saver import ProjectSaver
from report_formatter import ReportFormatter
from validation_service import ValidationService


class TrafficProject:
    """
    Переходный фасад над новой доменной моделью проекта.
    Сохраняет старый публичный интерфейс, но работает через
    ProjectLoader/AnalysisService/ProjectSaver.
    """

    def __init__(self, json_file_path, report_path_txt="analysis_report.txt"):
        self.json_path = json_file_path
        self.report_path = report_path_txt
        self.loader = ProjectLoader()
        self.saver = ProjectSaver()
        self.analysis_service = AnalysisService()
        self.validation_service = ValidationService()

        self.project = None
        self.links = {}
        self.routes = []
        self.pcu_coeffs = {}
        self.project_name = ""

    def load_data(self):
        """Загружает проект из нового или старого JSON-формата."""
        if not os.path.exists(self.json_path):
            print(f"Ошибка: Файл '{self.json_path}' не найден!")
            return False

        try:
            self.project = self.loader.load(self.json_path)
            validation_errors = self.validation_service.validate_project(self.project)
            if validation_errors:
                print("Обнаружены ошибки проекта:")
                for error in validation_errors:
                    print(f" - {error}")
            self._sync_legacy_views()
            print(f"Загружено: {len(self.links)} ссылок, {len(self.routes)} маршрутов.")
            return True
        except Exception as e:
            print(f"Ошибка при загрузке данных: {e}")
            return False

    def run_full_analysis(self):
        if self.project is None:
            raise RuntimeError("Проект не загружен. Сначала вызовите load_data().")

        report_data = self.analysis_service.analyze_project(self.project)
        self._sync_legacy_views()
        return report_data

    def export_report(self, report_data):
        """Сохраняет текстовый отчет."""
        formatter = ReportFormatter(self.project_name, report_data)
        report_content = formatter.generate_report()

        try:
            with open(self.report_path, "w", encoding="utf-8") as f:
                f.write(report_content)
            print(f"Текстовый отчет сохранен в '{self.report_path}'")
        except Exception as e:
            print(f"Ошибка записи отчета: {e}")

    def export_project(self, output_path):
        """Сохраняет проект в новом едином формате."""
        if self.project is None:
            raise RuntimeError("Проект не загружен. Сначала вызовите load_data().")
        self.saver.save(self.project, output_path)

    def export_json_for_viz(self, output_path="viz_data.json"):
        """
        Сохраняет данные для traffic_viz.py.
        Визуализатор пока использует отдельную view-модель, но теперь она
        строится из единого Project.
        """
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
            "L6_A_PED_TO_RING": [82.897, 55.049, 82.891, 55.049],
        }

        coords_source = DEFAULT_MAP.copy()
        if os.path.exists("saved_positions.json"):
            try:
                with open("saved_positions.json", "r", encoding="utf-8") as f:
                    coords_source.update(json.load(f))
            except Exception:
                pass

        links_data_json = []
        for link in self.project.network.links.values():
            coords = self._resolve_link_coords(link, coords_source)
            links_data_json.append(
                {
                    "id": link.id,
                    "name": link.name,
                    "results": link.results,
                    "coords": coords,
                }
            )

        routes_json = [route.results for route in self.project.network.routes.values() if route.results]

        full_json = {
            "project_name": self.project_name,
            "links": links_data_json,
            "routes": routes_json,
        }

        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(full_json, f, indent=2, ensure_ascii=False)
            print(f"JSON для визуализации обновлен: {output_path}")
        except Exception as e:
            print(f"Ошибка сохранения JSON: {e}")

    def _resolve_link_coords(self, link, coords_source):
        if link.coords:
            if link.coords.get("type") == "polyline":
                return link.coords
            if {"lon_start", "lat_start", "lon_end", "lat_end"} <= set(link.coords.keys()):
                return link.coords

        raw = coords_source.get(link.id, [0, 0, 0, 0])
        if isinstance(raw, dict):
            return raw
        return {
            "lon_start": raw[0],
            "lat_start": raw[1],
            "lon_end": raw[2],
            "lat_end": raw[3],
        }

    def _sync_legacy_views(self):
        """Поддерживает старые поля класса, пока остальной код не переведен."""
        self.project_name = self.project.project_name
        self.pcu_coeffs = self.project.pcu_coefficients
        self.links = self.project.network.links
        self.routes = list(self.project.network.routes.values())
