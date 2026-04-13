from road_sections import TARGET_LOS_VC, BASE_SPEED_KPH


class ReportFormatter:
    """Генерирует финальный отчет на основе данных анализа."""

    def __init__(self, project_name, report_data):
        self.project_name = project_name
        self.data = report_data

    def generate_report(self):
        """Собирает все части отчета."""
        report_lines = []
        report_lines.append("=" * 80)
        report_lines.append(f"АВТОМАТИЗИРОВАННЫЙ ОТЧЕТ ПО ТРАНСПОРТНОМУ МОДЕЛИРОВАНИЮ")
        report_lines.append(f"Проект: {self.project_name}")
        report_lines.append(f"Базовая скорость: {BASE_SPEED_KPH} км/ч")
        report_lines.append(f"Целевой уровень обслуживания (V/C): <= {TARGET_LOS_VC}")
        report_lines.append("=" * 80 + "\n")

        report_lines.append(self._format_links_analysis())
        report_lines.append(self._format_optimization_proposals())
        report_lines.append(self._format_routes_analysis())

        return "\n".join(report_lines)

    def _format_links_analysis(self):
        """Форматирует локальный анализ по направленным ссылкам."""
        output = ["\n### ЭТАП 1: ЛОКАЛЬНЫЙ АНАЛИЗ (V/C и LOS) ПО НАПРАВЛЕНИЯМ ###"]
        output.append("-" * 70)

        for link in self.data['Links_Analysis']:
            output.append(f"[{link['id']}] {link['name']} | Тип: {link['type']}")
            output.append(f"  V (факт): {link['V']:.0f} прив.ед/ч | C (макс): {link['C_initial']:.0f} прив.ед/ч")
            output.append(f"  Загрузка V/C: **{link['VC_ratio']:.3f}** | Уровень обслуживания (LOS): **{link['LOS']}**")

            if link['type'] == 'Intersection':
                factors = f"f_w={link['f_w']:.3f}, f_g={link['f_g']:.3f}, f_HV={link['f_HV']:.3f}, f_p={link['f_p']:.3f}"
                if 'f_R' in link and link['f_R'] < 1.0:
                    factors += f", f_R={link['f_R']:.3f} (Кольцо)"
                output.append(f"  [Детали S] S скорр.: {link['S_corrected']:.0f} | Факторы: {factors}")

                g_total = link['Green_g'] + link['Green_g_others']
                g_remaining = link['Cycle_T'] - g_total

                output.append(
                    f"  [Цикл] T={link['Cycle_T']} с. | g_наш={link['Green_g']} с. | g_др={link['Green_g_others']} с.")
                output.append(f"  [Проверка] Занято: {g_total} с. | Потерянное время (жёлтый/пустой): {g_remaining} с.")

            elif link['type'] == 'StraightRoad':
                output.append(
                    f"  [Детали C] C_баз/пол.: {link['C_base_per_lane']:.0f} | Факторы: f_w={link['f_w']:.3f}, f_g={link['f_g']:.3f}, f_HV={link['f_HV']:.3f}, f_p={link['f_p']:.3f}")

            output.append("-" * 70)

        return "\n".join(output)

    def _format_optimization_proposals(self):
        """Форматирует предложения по оптимизации."""
        output = ["\n### ЭТАП 2: ПРОЕКТНЫЕ ПРЕДЛОЖЕНИЯ (Оптимизация) ###"]
        output.append("-" * 70)

        problem_found = False
        for link in self.data['Links_Analysis']:
            if 'Optimization_Proposal' in link:
                problem_found = True
                output.append(f"!!! АНАЛИЗ ПРОБЛЕМНОЙ ССЫЛКИ: {link['name']}")
                output.append(f"  - LOS ДО: {link['LOS']} (V/C = {link['VC_ratio']:.3f})")
                output.append(f"  - ПРЕДЛОЖЕНИЕ: {link['Optimization_Proposal']}")
                output.append(f"  - Новый V/C: {link['VC_optimized']:.3f} | Новый LOS: {link['LOS_optimized']}")
                output.append("-" * 70)

        if not problem_found:
            output.append("Все направленные ссылки работают в штатном режиме (LOS A-D). Оптимизация не требуется.")
            output.append("-" * 70)

        return "\n".join(output)

    def _format_routes_analysis(self):
        """Форматирует анализ маршрутов."""
        output = ["\n### ЭТАП 3: СИСТЕМНЫЙ АНАЛИЗ ПО МАРШРУТАМ (КОРИДОР) ###"]
        output.append("-" * 70)

        for route in self.data['Routes_Analysis']:
            output.append(f"*** МАРШРУТ: {route['name']} ({route['total_length_km']:.2f} км) ***")

            for link_detail in route['links_detail']:
                output.append(
                    f"  -> {link_detail['link_id']} (LOS {link_detail['LOS']}, V/C={link_detail['VC_ratio']:.3f}): Доп. задержка: {link_detail['Delay_sec']:.1f} сек")

            output.append("\n    --- ИТОГОВЫЕ KPI МАРШРУТА ---")
            output.append(f"    1. Суммарная доп. задержка: {route['total_delay_sec']:.1f} сек/прив.авт")
            output.append(f"    2. Общее время проезда: {route['total_travel_time_sec']:.1f} сек/прив.авт")
            output.append(f"    3. Средняя скорость по маршруту: **{route['avg_speed_kph']:.1f} км/ч**")
            output.append("-" * 70)

        return "\n".join(output)