from abc import ABC, abstractmethod
import math

# Конфигурация
LOS_THRESHOLDS = {'A': 0.20, 'B': 0.45, 'C': 0.70, 'D': 0.90, 'E': 1.00, 'F': float('inf')}
TARGET_LOS_VC = 0.70
BASE_SPEED_KPH = 60
B_COEFFICIENT = 4


class RoadSection(ABC):
    def __init__(self, section_id, name, traffic_counts, pcu_coeffs, length_km):
        self.id = section_id
        self.name = name
        self.traffic_counts = traffic_counts
        self.pcu_coeffs = pcu_coeffs
        self.length = length_km
        self.V = self._calculate_equivalent_intensity()
        self.C = 0.0
        self.vc_ratio = 0.0
        self.los = 'UNDEFINED'
        self.analysis_data = {}

    def _calculate_equivalent_intensity(self):
        total_pcu = 0.0
        for v_type, count in self.traffic_counts.items():
            coeff = self.pcu_coeffs.get(v_type, 1.0)
            total_pcu += count * coeff
        return total_pcu

    def _determine_los(self, vc):
        for grade, threshold in LOS_THRESHOLDS.items():
            if vc <= threshold: return grade
        return 'F'

    def analyze_performance(self):
        """Полный цикл анализа: пропускная способность -> загрузка -> LOS -> задержка"""
        self.calculate_capacity()
        if self.C <= 0:
            self.vc_ratio = 99.0
        else:
            self.vc_ratio = self.V / self.C

        self.los = self._determine_los(self.vc_ratio)

        self.analysis_data.update({
            'V': round(self.V, 0),
            'C_initial': round(self.C, 0),
            'VC_ratio': round(self.vc_ratio, 3),
            'LOS': self.los,
            'Delay_sec': round(self.calculate_delay(), 1),
            'Length_km': self.length
        })

    def calculate_delay(self):
        if self.C <= 0: return float('inf')
        T0 = (self.length / BASE_SPEED_KPH) * 3600
        if self.vc_ratio < 0.99:
            return T0 * 0.25 * (self.vc_ratio ** B_COEFFICIENT)
        return T0 * (10 + (self.vc_ratio * 5))

    @abstractmethod
    def calculate_capacity(self):
        pass

    @abstractmethod
    def optimize(self):
        pass

    def get_report_data(self):
        res = {'id': self.id, 'name': self.name, 'type': self.__class__.__name__}
        res.update(self.analysis_data)
        return res


class StraightRoad(RoadSection):
    def __init__(self, section_id, name, traffic_counts, pcu_coeffs, length_km,
                 lanes_total, lanes_bus, capacity_per_lane_base,
                 lane_width_m, grade_percent, parking_present, heavy_vehicles_percent):
        super().__init__(section_id, name, traffic_counts, pcu_coeffs, length_km)
        self.lanes_total = lanes_total
        self.lanes_bus = lanes_bus
        self.capacity_base_per_lane = capacity_per_lane_base
        self.w, self.G, self.P_exist, self.P_HV = lane_width_m, grade_percent, parking_present, heavy_vehicles_percent

    def calculate_capacity(self):
        f_w = 1.0 + 0.1 * (self.w - 3.6)
        f_g = 1.0 - (self.G / 100.0)
        f_HV = 1.0 / (1.0 + self.P_HV * (2.0 - 1))
        f_p = 0.95 if self.P_exist else 1.0
        effective_lanes = max(0, self.lanes_total - self.lanes_bus)
        # Пропускная способность = Полосы * База * Коэффициенты
        self.C = effective_lanes * self.capacity_base_per_lane * f_w * f_g * f_HV * f_p

        self.analysis_data.update({
            'f_w': round(f_w, 3), 'f_g': round(f_g, 3), 'f_HV': round(f_HV, 3), 'f_p': round(f_p, 3),
            'C_base_per_lane': self.capacity_base_per_lane
        })

    def optimize(self):
        if self.vc_ratio <= TARGET_LOS_VC: return None
        if self.C <= 0:
            return {
                'proposal': "CRITICAL: capacity is zero; check closure, lanes, or base capacity.",
                'C_new': self.C,
                'vc_new': self.vc_ratio,
                'los_new': self.los
            }

        # РАСЧЕТ ИЗ ОРИГИНАЛА: Сколько полос нужно добавить?
        required_C = self.V / TARGET_LOS_VC
        effective_lanes = self.lanes_total - self.lanes_bus
        # Требуемое кол-во полос = (Требуемая C / Текущая C) * Текущие полосы
        required_lanes = (required_C / self.C) * effective_lanes
        additional_lanes = required_lanes - effective_lanes

        return {
            'proposal': f"РАСШИРЕНИЕ: Добавить {additional_lanes:.1f} полосы.",
            'C_new': required_C,
            'vc_new': TARGET_LOS_VC,
            'los_new': self._determine_los(TARGET_LOS_VC)
        }


class Intersection(RoadSection):
    def __init__(self, section_id, name, traffic_counts, pcu_coeffs, length_km,
                 cycle_time, green_time, saturation_flow_base, lanes_count,
                 lane_width_m, grade_percent, parking_present,
                 heavy_vehicles_percent, is_ring_approach=False, g_others=0):
        super().__init__(section_id, name, traffic_counts, pcu_coeffs, length_km)
        self.T, self.g, self.g_others = cycle_time, green_time, g_others
        self.S0, self.N = saturation_flow_base, lanes_count
        self.w, self.G, self.P_exist, self.P_HV = lane_width_m, grade_percent, parking_present, heavy_vehicles_percent
        self.is_ring = is_ring_approach

    def calculate_capacity(self):
        f_w = 1.0 + 0.1 * (self.w - 3.6)
        f_g = 1.0 - (self.G / 100.0)
        f_HV = 1.0 / (1.0 + self.P_HV * (2.5 - 1))
        f_p = 0.90 if self.P_exist else 1.0
        f_R = 0.90 if self.is_ring or "ENTRY" in self.id.upper() else 1.0

        self.S = self.S0 * f_w * f_g * f_HV * f_p * f_R
        self.C = self.S * self.N * (self.g / self.T)

        self.analysis_data.update({
            'S_corrected': round(self.S, 0),
            'f_w': round(f_w, 3), 'f_g': round(f_g, 3), 'f_HV': round(f_HV, 3),
            'f_p': round(f_p, 3), 'f_R': round(f_R, 3),
            'Cycle_T': self.T, 'Green_g': self.g, 'Green_g_others': self.g_others
        })

    def optimize(self):
        if self.vc_ratio <= TARGET_LOS_VC: return None

        # РАСЧЕТ ИЗ ОРИГИНАЛА: Сколько секунд зеленого нужно?
        required_C = self.V / TARGET_LOS_VC
        required_g = (required_C / (self.S * self.N)) * self.T

        # Проверка на критическую перегрузку (нельзя дать больше зеленого, чем весь цикл минус другие фазы)
        if required_g >= (self.T - self.g_others):
            return {
                'proposal': "КРИТИЧЕСКАЯ ПЕРЕГРУЗКА: Требуется реконструкция или изменение геометрии.",
                'C_new': self.C,
                'vc_new': self.vc_ratio,
                'los_new': self.los
            }

        return {
            'proposal': f"СВЕТОФОРНОЕ РЕГУЛИРОВАНИЕ: Увеличить зеленое время с {self.g} сек до {required_g:.1f} сек.",
            'C_new': required_C,
            'vc_new': TARGET_LOS_VC,
            'los_new': self._determine_los(TARGET_LOS_VC)
        }

    def get_report_data(self):
        data = super().get_report_data()
        data['Cycle_T'] = self.T
        data['Green_g'] = self.g
        data['Green_g_others'] = self.g_others
        return data
