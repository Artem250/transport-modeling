from project_manager import TrafficProject

if __name__ == "__main__":
    FILE_PATH = "novosibirsk_analysis.json"

    # Создаем проект
    project = TrafficProject(FILE_PATH)

    if project.load_data():
        # Запускаем расчеты (это заполнит внутренние структуры V/C, LOS и т.д.)
        report_data = project.run_full_analysis()

        # 1. Генерируем текстовый отчет (как было)
        project.export_report(report_data)

        # 2. Генерируем JSON для визуализации (НОВОЕ)
        project.export_json_for_viz("viz_data.json")