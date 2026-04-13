from road_sections import BASE_SPEED_KPH


class CorridorRoute:
    """Описывает маршрут, состоящий из последовательности RoadSection, и рассчитывает интегральные KPI."""

    def __init__(self, route_id, name, links):
        self.id = route_id
        self.name = name
        self.links = links
        self.total_length_km = sum(link.length for link in links)
        self.total_delay_sec = 0.0
        self.total_travel_time_sec = 0.0
        self.avg_speed_kph = 0.0

    def calculate_kpi(self):
        """Рассчитывает суммарные показатели эффективности для всего маршрута."""

        self.total_delay_sec = sum(link.analysis_data['Delay_sec'] for link in self.links)

        # Базовое время в пути (без задержек V/C)
        base_travel_time_sec = (self.total_length_km / BASE_SPEED_KPH) * 3600

        # Общее время в пути = Базовое время + Суммарная задержка
        self.total_travel_time_sec = base_travel_time_sec + self.total_delay_sec

        # Средняя скорость = Общая дистанция / Общее время (в часах)
        if self.total_travel_time_sec > 0:
            total_travel_time_hours = self.total_travel_time_sec / 3600
            self.avg_speed_kph = self.total_length_km / total_travel_time_hours
        else:
            self.avg_speed_kph = BASE_SPEED_KPH

    def get_report_data(self):
        """Возвращает данные маршрута для отчета."""
        return {
            'id': self.id,
            'name': self.name,
            'total_length_km': round(self.total_length_km, 2),
            'total_delay_sec': round(self.total_delay_sec, 1),
            'total_travel_time_sec': round(self.total_travel_time_sec, 1),
            'avg_speed_kph': round(self.avg_speed_kph, 1),
            'links_detail': [
                {'link_id': link.id, 'LOS': link.los, 'VC_ratio': round(link.vc_ratio, 3),
                 'Delay_sec': round(link.analysis_data['Delay_sec'], 1)}
                for link in self.links
            ]
        }