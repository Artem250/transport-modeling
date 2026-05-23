from project_manager import TrafficProject


if __name__ == "__main__":
    file_path = "novosibirsk_analysis.json"
    modern_export_path = "network_project_test.json"

    project = TrafficProject(file_path)
    if project.load_data():
        report_data = project.run_full_analysis()
        project.export_report(report_data)
        project.export_project(modern_export_path)
        project.export_json_for_viz("viz_data.json")
