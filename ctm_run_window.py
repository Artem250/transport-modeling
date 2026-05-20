from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class RunnerThread(QThread):
    output = pyqtSignal(str)
    finished_ok = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, command: list[str], cwd: str | None = None):
        super().__init__()
        self.command = command
        self.cwd = cwd

    def run(self) -> None:
        try:
            process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                self.output.emit(line.rstrip())
            return_code = process.wait()
            if return_code == 0:
                self.finished_ok.emit()
            else:
                self.failed.emit(f"Process finished with return code {return_code}")
        except Exception as exc:  # pragma: no cover - GUI worker safety
            self.failed.emit(str(exc))


class CTMRunWindow(QMainWindow):
    """Small control window for repeatable CTM experiments.

    The heavy work is delegated to ctm_experiment_runner.py. This window is only a
    convenient parameter editor, so it does not duplicate CTM logic.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CTM scenario runner")
        self.resize(920, 720)
        self.worker: RunnerThread | None = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        files_group = QGroupBox("Файлы")
        files_layout = QFormLayout(files_group)
        self.project_edit = QLineEdit("osm_network_project_map_nstu.json")
        self.output_edit = QLineEdit("ctm_experiments")
        self.map_edit = QLineEdit("map_nstu.osm")
        self.project_btn = QPushButton("Выбрать...")
        self.output_btn = QPushButton("Выбрать...")
        self.map_btn = QPushButton("Выбрать...")
        self.project_btn.clicked.connect(self.choose_project)
        self.output_btn.clicked.connect(self.choose_output_dir)
        self.map_btn.clicked.connect(self.choose_map)
        files_layout.addRow("Проект JSON:", self._row(self.project_edit, self.project_btn))
        files_layout.addRow("Папка результатов:", self._row(self.output_edit, self.output_btn))
        files_layout.addRow("OSM-подложка:", self._row(self.map_edit, self.map_btn))
        layout.addWidget(files_group)

        params_group = QGroupBox("Основные параметры CTM")
        params_layout = QFormLayout(params_group)
        self.dt_edit = QLineEdit("0.5")
        self.minutes_edit = QLineEdit("50")
        self.snapshot_edit = QLineEdit("10")
        self.cell_length_edit = QLineEdit("15.0")
        self.inflow_edit = QLineEdit("6500.0")
        params_layout.addRow("dt, сек:", self.dt_edit)
        params_layout.addRow("Длительность, мин:", self.minutes_edit)
        params_layout.addRow("Шаг сохранения истории, сек:", self.snapshot_edit)
        params_layout.addRow("Целевая длина ячейки, м:", self.cell_length_edit)
        params_layout.addRow("Общий входной поток, авт/ч:", self.inflow_edit)
        layout.addWidget(params_group)

        incident_group = QGroupBox("Аварийный участок / bottleneck")
        incident_layout = QFormLayout(incident_group)
        self.incident_link_edit = QLineEdit("")
        self.incident_link_edit.setPlaceholderText("пусто = выбрать автоматически")
        self.incident_start_edit = QLineEdit("300")
        self.incident_end_edit = QLineEdit("900")
        self.incident_cap_edit = QLineEdit("0.1")
        self.incident_speed_edit = QLineEdit("1.0")
        self.incident_blocked_lanes_edit = QLineEdit("1")
        self.added_lane_delta_edit = QLineEdit("1")
        self.fifo_strength_edit = QLineEdit("1.0")
        incident_layout.addRow("Link аварии:", self.incident_link_edit)
        incident_layout.addRow("Начало аварии, сек:", self.incident_start_edit)
        incident_layout.addRow("Конец аварии, сек:", self.incident_end_edit)
        incident_layout.addRow("Заблокировано полос:", self.incident_blocked_lanes_edit)
        incident_layout.addRow("Добавить полос в mitigation:", self.added_lane_delta_edit)
        incident_layout.addRow("FIFO strength:", self.fifo_strength_edit)
        incident_layout.addRow("Коэффициент capacity:", self.incident_cap_edit)
        incident_layout.addRow("Коэффициент скорости:", self.incident_speed_edit)
        layout.addWidget(incident_group)

        actions = QHBoxLayout()
        self.run_btn = QPushButton("Запустить 3 сценария")
        self.run_btn.clicked.connect(self.run_experiments)
        self.open_viz_btn = QPushButton("Открыть визуализацию результата")
        self.open_viz_btn.clicked.connect(self.open_visualizer)
        self.result_combo = QComboBox()
        self.result_combo.addItems([
            "ctm_results_baseline.json",
            "ctm_results_lane_blockage.json",
            "ctm_results_lane_blockage_added_lane.json",
        ])
        actions.addWidget(self.run_btn)
        actions.addWidget(QLabel("Файл для визуализации:"))
        actions.addWidget(self.result_combo)
        actions.addWidget(self.open_viz_btn)
        layout.addLayout(actions)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(QLabel("Лог:"))
        layout.addWidget(self.log, 1)

        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.summary.setFixedHeight(120)
        self.summary.setPlainText(
            "Runner создаёт baseline, lane_blockage и lane_blockage_added_lane; "
            "сохраняет JSON, CSV-метрики и PNG-графики. "
            "Для ВКР особенно полезны ctm_metrics.csv, plot_incident_link_density.png, "
            "plot_incident_link_flow.png и plot_source_queue.png."
        )
        layout.addWidget(QLabel("Что получится:"))
        layout.addWidget(self.summary)

    def _row(self, *widgets):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        for widget in widgets:
            layout.addWidget(widget)
        return row

    def choose_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите проект", "", "JSON (*.json);;All files (*)")
        if path:
            self.project_edit.setText(path)

    def choose_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Выберите папку результатов")
        if path:
            self.output_edit.setText(path)

    def choose_map(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Выберите OSM-файл", "", "OSM (*.osm);;XML (*.xml);;All files (*)")
        if path:
            self.map_edit.setText(path)

    def validate_float(self, edit: QLineEdit, label: str) -> float:
        try:
            return float(edit.text().strip())
        except ValueError as exc:
            raise ValueError(f"{label}: должно быть числом") from exc

    def validate_int(self, edit: QLineEdit, label: str) -> int:
        try:
            return int(edit.text().strip())
        except ValueError as exc:
            raise ValueError(f"{label}: должно быть целым числом") from exc

    def run_experiments(self) -> None:
        try:
            dt = self.validate_float(self.dt_edit, "dt")
            minutes = self.validate_int(self.minutes_edit, "Длительность")
            snapshot = self.validate_int(self.snapshot_edit, "Шаг сохранения")
            cell_length = self.validate_float(self.cell_length_edit, "Длина ячейки")
            inflow = self.validate_float(self.inflow_edit, "Входной поток")
            incident_start = self.validate_float(self.incident_start_edit, "Начало аварии")
            incident_end = self.validate_float(self.incident_end_edit, "Конец аварии")
            incident_cap = self.validate_float(self.incident_cap_edit, "Коэффициент capacity")
            incident_speed = self.validate_float(self.incident_speed_edit, "Коэффициент скорости")
            incident_blocked_lanes = self.validate_int(self.incident_blocked_lanes_edit, "Заблокировано полос")
            added_lane_delta = self.validate_int(self.added_lane_delta_edit, "Добавить полос")
            fifo_strength = self.validate_float(self.fifo_strength_edit, "FIFO strength")
            if dt <= 0 or minutes <= 0 or snapshot <= 0 or cell_length <= 0 or inflow < 0:
                raise ValueError("dt, длительность, шаг истории и длина ячейки должны быть положительными; входной поток неотрицательный")
            if incident_end <= incident_start:
                raise ValueError("Конец аварии должен быть позже начала")
            if not 0 <= incident_cap <= 1:
                raise ValueError("Коэффициент capacity должен быть в [0, 1]")
            if incident_speed < 0:
                raise ValueError("Коэффициент скорости не может быть отрицательным")
            if incident_blocked_lanes < 0:
                raise ValueError("Количество заблокированных полос не может быть отрицательным")
            if added_lane_delta < 0:
                raise ValueError("Добавляемое число полос не может быть отрицательным")
            if not 0 <= fifo_strength <= 1:
                raise ValueError("FIFO strength должен быть в [0, 1]")
        except ValueError as exc:
            QMessageBox.warning(self, "Параметры", str(exc))
            return

        command = [
            sys.executable,
            "ctm_experiment_runner.py",
            "--project", self.project_edit.text().strip(),
            "--output-dir", self.output_edit.text().strip(),
            "--dt", str(dt),
            "--minutes", str(minutes),
            "--snapshot-sec", str(snapshot),
            "--cell-length", str(cell_length),
            "--inflow", str(inflow),
            "--incident-start", str(incident_start),
            "--incident-end", str(incident_end),
            "--incident-capacity-factor", str(incident_cap),
            "--incident-speed-factor", str(incident_speed),
            "--incident-blocked-lanes", str(incident_blocked_lanes),
            "--added-lane-delta", str(added_lane_delta),
            "--fifo-strength", str(fifo_strength),
        ]
        incident_link = self.incident_link_edit.text().strip()
        if incident_link:
            command.extend(["--incident-link", incident_link])

        self.log.clear()
        self.log.append("$ " + " ".join(command))
        self.run_btn.setEnabled(False)
        self.worker = RunnerThread(command)
        self.worker.output.connect(self.log.append)
        self.worker.finished_ok.connect(self.on_finished_ok)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_finished_ok(self) -> None:
        self.run_btn.setEnabled(True)
        self.log.append("\nГотово. Результаты сохранены.")
        QMessageBox.information(self, "CTM", "Сценарии успешно рассчитаны.")

    def on_failed(self, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.log.append("\nОшибка: " + message)
        QMessageBox.critical(self, "CTM", message)

    def open_visualizer(self) -> None:
        output_dir = Path(self.output_edit.text().strip())
        result_file = output_dir / self.result_combo.currentText()
        if not result_file.exists():
            QMessageBox.warning(self, "Визуализация", f"Файл не найден:\n{result_file}")
            return
        command = [
            sys.executable,
            "ctm_dynamic_viz.py",
            "--map", self.map_edit.text().strip(),
            "--results", str(result_file),
        ]
        subprocess.Popen(command)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = CTMRunWindow()
    window.show()
    sys.exit(app.exec_())
