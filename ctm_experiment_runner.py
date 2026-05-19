from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ctm_network_simulator import CTMScenarioConfig, CTMSimulator
from project_loader import ProjectLoader
from project_saver import ProjectSaver

try:
    import matplotlib.pyplot as plt
except Exception:  # pragma: no cover - plotting is optional in headless envs
    plt = None


@dataclass(frozen=True)
class ExperimentSpec:
    name: str
    fifo_strength: float
    incident_capacity_factor: float
    incident_speed_factor: float = 1.0


def choose_default_incident_link(project_file: str, base_config: CTMScenarioConfig) -> str | None:
    """Use the simulator's current rule to choose the representative incident link."""

    project = ProjectLoader().load(project_file)
    probe = CTMSimulator(project, base_config)
    return probe.incident_link_id


def run_experiment(
    *,
    project_file: str,
    output_dir: Path,
    spec: ExperimentSpec,
    dt_seconds: float,
    simulation_minutes: int,
    snapshot_interval_sec: int,
    cell_length_target_m: float,
    inflow_veh_per_hour: float,
    incident_link_id: str | None,
    incident_start_sec: float,
    incident_end_sec: float,
) -> tuple[dict[str, Any], Path]:
    project = ProjectLoader().load(project_file)
    project.metadata["ctm_experiment_name"] = spec.name

    config = CTMScenarioConfig(
        dt_seconds=dt_seconds,
        simulation_minutes=simulation_minutes,
        snapshot_interval_sec=snapshot_interval_sec,
        cell_length_target_m=cell_length_target_m,
        inflow_veh_per_hour=inflow_veh_per_hour,
        incident_link_id=incident_link_id,
        incident_start_sec=incident_start_sec,
        incident_end_sec=incident_end_sec,
        incident_capacity_factor=spec.incident_capacity_factor,
        incident_speed_factor=spec.incident_speed_factor,
        fifo_strength=spec.fifo_strength,
    )

    simulator = CTMSimulator(project, config)
    simulator.run()

    result_path = output_dir / f"ctm_results_{spec.name}.json"
    ProjectSaver().save(project, str(result_path))

    metrics = collect_metrics(project, spec.name)
    return metrics, result_path


def collect_metrics(project, scenario_name: str) -> dict[str, Any]:
    sim = project.metadata.get("ctm_simulation", {}) or {}
    incident = project.metadata.get("ctm_incident", {}) or {}
    movement_summary = project.metadata.get("ctm_movement_summary", {}) or {}

    max_density = 0.0
    avg_density_sum = 0.0
    avg_density_count = 0
    max_flow = 0.0
    max_source_queue = 0.0
    incident_link_max_density = 0.0
    incident_link_avg_density = 0.0

    incident_link_id = incident.get("link_id")
    for link in project.network.links.values():
        history = link.results.get("history_cells_density_pcu_km", []) or []
        for snapshot in history:
            if not snapshot:
                continue
            max_density = max(max_density, max(snapshot))
            avg_density_sum += sum(snapshot) / len(snapshot)
            avg_density_count += 1
        for flow in link.results.get("history_flow_veh_h", []) or []:
            max_flow = max(max_flow, float(flow))
        for queue in link.results.get("history_external_queue_pcu", []) or []:
            max_source_queue = max(max_source_queue, float(queue))

        if link.id == incident_link_id and history:
            per_snapshot_avg = [sum(snapshot) / len(snapshot) for snapshot in history if snapshot]
            incident_link_avg_density = sum(per_snapshot_avg) / len(per_snapshot_avg) if per_snapshot_avg else 0.0
            incident_link_max_density = max(max(snapshot) for snapshot in history if snapshot)

    return {
        "scenario_name": scenario_name,
        "fifo_strength": sim.get("fifo_strength"),
        "incident_link_id": incident_link_id or "",
        "incident_capacity_factor": incident.get("capacity_factor", ""),
        "incident_start_sec": incident.get("start_time_sec", ""),
        "incident_end_sec": incident.get("end_time_sec", ""),
        "total_generated_pcu": sim.get("total_generated_pcu", 0.0),
        "total_entered_pcu": sim.get("total_entered_pcu", 0.0),
        "total_exited_pcu": sim.get("total_exited_pcu", 0.0),
        "mass_in_network_pcu": sim.get("mass_in_network_pcu", 0.0),
        "total_external_queue_pcu": sim.get("total_external_queue_pcu", 0.0),
        "demand_balance_error_pcu": sim.get("demand_balance_error_pcu", 0.0),
        "conservation_error_pcu": sim.get("conservation_error_pcu", 0.0),
        "source_queue_balance_error_pcu": sim.get("source_queue_balance_error_pcu", 0.0),
        "sum_link_conservation_error_pcu": sim.get("sum_link_conservation_error_pcu", 0.0),
        "max_abs_link_conservation_error_pcu": sim.get("max_abs_link_conservation_error_pcu", 0.0),
        "max_density_pcu_km": round(max_density, 3),
        "avg_density_pcu_km": round(avg_density_sum / avg_density_count, 3) if avg_density_count else 0.0,
        "incident_link_max_density_pcu_km": round(incident_link_max_density, 3),
        "incident_link_avg_density_pcu_km": round(incident_link_avg_density, 3),
        "max_flow_veh_h": round(max_flow, 3),
        "max_source_queue_pcu": round(max_source_queue, 3),
        "movement_count": movement_summary.get("movement_count", 0),
        "short_connector_candidate_count": movement_summary.get("short_connector_candidate_count", 0),
    }


def write_metrics_csv(metrics: list[dict[str, Any]], path: Path) -> None:
    if not metrics:
        return
    fieldnames = list(metrics[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(metrics)


def write_movements_csv(project_file: Path, path: Path) -> None:
    project = ProjectLoader().load(str(project_file))
    movements = project.metadata.get("ctm_movements", []) or []
    if not movements:
        return
    fieldnames = sorted({key for movement in movements for key in movement.keys()})
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(movements)


def plot_experiments(result_files: dict[str, Path], output_dir: Path) -> None:
    if plt is None:
        print("matplotlib is not available; plots skipped")
        return

    loaded = {name: ProjectLoader().load(str(path)) for name, path in result_files.items()}
    incident_link_id = next(
        (
            project.metadata.get("ctm_incident", {}).get("link_id")
            for project in loaded.values()
            if project.metadata.get("ctm_incident", {}).get("link_id")
        ),
        None,
    )
    if not incident_link_id:
        return

    _plot_incident_density(loaded, incident_link_id, output_dir / "plot_incident_link_density.png")
    _plot_incident_flow(loaded, incident_link_id, output_dir / "plot_incident_link_flow.png")
    _plot_source_queue(loaded, output_dir / "plot_source_queue.png")


def _time_axis(length: int, snapshot_interval_sec: int) -> list[float]:
    return [i * snapshot_interval_sec / 60.0 for i in range(length)]


def _plot_incident_density(projects: dict[str, Any], link_id: str, path: Path) -> None:
    plt.figure()
    for name, project in projects.items():
        link = project.network.links.get(link_id)
        if link is None:
            continue
        history = link.results.get("history_cells_density_pcu_km", []) or []
        series = [sum(snapshot) / len(snapshot) if snapshot else 0.0 for snapshot in history]
        interval = int(project.metadata.get("ctm_scenario_config", {}).get("snapshot_interval_sec", 60))
        plt.plot(_time_axis(len(series), interval), series, label=name)
    plt.xlabel("Время, мин")
    plt.ylabel("Средняя плотность на аварийном link, pcu/km")
    plt.title(f"Динамика плотности на link {link_id}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def _plot_incident_flow(projects: dict[str, Any], link_id: str, path: Path) -> None:
    plt.figure()
    for name, project in projects.items():
        link = project.network.links.get(link_id)
        if link is None:
            continue
        series = [float(v) for v in (link.results.get("history_flow_veh_h", []) or [])]
        interval = int(project.metadata.get("ctm_scenario_config", {}).get("snapshot_interval_sec", 60))
        plt.plot(_time_axis(len(series), interval), series, label=name)
    plt.xlabel("Время, мин")
    plt.ylabel("Выходной поток, veh/h")
    plt.title(f"Выходной поток через link {link_id}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def _plot_source_queue(projects: dict[str, Any], path: Path) -> None:
    plt.figure()
    for name, project in projects.items():
        series_by_source = []
        for link in project.network.links.values():
            queue = link.results.get("history_external_queue_pcu", []) or []
            if queue:
                series_by_source.append([float(v) for v in queue])
        if not series_by_source:
            continue
        max_len = max(len(series) for series in series_by_source)
        total_queue = []
        for i in range(max_len):
            total_queue.append(sum(series[i] if i < len(series) else series[-1] for series in series_by_source))
        interval = int(project.metadata.get("ctm_scenario_config", {}).get("snapshot_interval_sec", 60))
        plt.plot(_time_axis(len(total_queue), interval), total_queue, label=name)
    plt.xlabel("Время, мин")
    plt.ylabel("Внешняя очередь источников, pcu")
    plt.title("Накопление неудовлетворенного входного спроса")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run baseline/incident/FIFO CTM experiments.")
    parser.add_argument("--project", default="osm_network_project_map_nstu.json")
    parser.add_argument("--output-dir", default="ctm_experiments")
    parser.add_argument("--dt", type=float, default=0.5)
    parser.add_argument("--minutes", type=int, default=100)
    parser.add_argument("--snapshot-sec", type=int, default=60)
    parser.add_argument("--cell-length", type=float, default=15.0)
    parser.add_argument("--inflow", type=float, default=475.0)
    parser.add_argument("--incident-link", default=None)
    parser.add_argument("--incident-start", type=float, default=300.0)
    parser.add_argument("--incident-end", type=float, default=900.0)
    parser.add_argument("--incident-capacity-factor", type=float, default=0.1)
    parser.add_argument("--incident-speed-factor", type=float, default=1.0)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    base_config = CTMScenarioConfig(
        dt_seconds=args.dt,
        simulation_minutes=args.minutes,
        snapshot_interval_sec=args.snapshot_sec,
        cell_length_target_m=args.cell_length,
        inflow_veh_per_hour=args.inflow,
        incident_link_id=args.incident_link,
        incident_start_sec=args.incident_start,
        incident_end_sec=args.incident_end,
        incident_capacity_factor=1.0,
        incident_speed_factor=args.incident_speed_factor,
        fifo_strength=0.0,
    )
    incident_link_id = args.incident_link or choose_default_incident_link(args.project, base_config)
    print(f"Incident/control link: {incident_link_id}")

    specs = [
        ExperimentSpec("baseline", fifo_strength=0.0, incident_capacity_factor=1.0),
        ExperimentSpec("incident_nonfifo", fifo_strength=0.0, incident_capacity_factor=args.incident_capacity_factor),
        ExperimentSpec("incident_fifo", fifo_strength=1.0, incident_capacity_factor=args.incident_capacity_factor),
    ]

    metrics: list[dict[str, Any]] = []
    result_files: dict[str, Path] = {}
    for spec in specs:
        print(f"\n=== Running {spec.name} ===")
        row, result_path = run_experiment(
            project_file=args.project,
            output_dir=output_dir,
            spec=spec,
            dt_seconds=args.dt,
            simulation_minutes=args.minutes,
            snapshot_interval_sec=args.snapshot_sec,
            cell_length_target_m=args.cell_length,
            inflow_veh_per_hour=args.inflow,
            incident_link_id=incident_link_id,
            incident_start_sec=args.incident_start,
            incident_end_sec=args.incident_end,
        )
        metrics.append(row)
        result_files[spec.name] = result_path
        write_movements_csv(result_path, output_dir / f"ctm_movements_{spec.name}.csv")

    write_metrics_csv(metrics, output_dir / "ctm_metrics.csv")
    plot_experiments(result_files, output_dir)
    print(f"\nExperiment outputs saved to: {output_dir}")


if __name__ == "__main__":
    main()
