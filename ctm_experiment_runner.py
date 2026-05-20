from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
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
    incident_capacity_factor: float = 1.0
    incident_speed_factor: float = 1.0
    incident_blocked_lanes: int | None = None
    lane_delta_by_link: dict[str, int] = field(default_factory=dict)


def choose_default_incident_link(project_file: str, base_config: CTMScenarioConfig) -> str | None:
    """Use the simulator's current rule to choose the representative incident link."""

    project = ProjectLoader().load(project_file)
    probe = CTMSimulator(project, base_config)
    return probe.incident_link_id


def apply_lane_changes(project, lane_delta_by_link: dict[str, int]) -> None:
    for link_id, delta in lane_delta_by_link.items():
        link = project.network.links.get(link_id)
        if link is None:
            raise ValueError(f"lane_delta_by_link references unknown link: {link_id}")

        old_lanes = max(int(link.parameters.get("lanes_total", 1) or 1), 1)
        new_lanes = max(1, old_lanes + int(delta))

        link.parameters["lanes_total_base"] = old_lanes
        link.parameters["lanes_total"] = new_lanes
        link.parameters["lanes_total_scenario"] = new_lanes
        link.metadata["lane_scenario_delta"] = int(delta)


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
    apply_lane_changes(project, spec.lane_delta_by_link)

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
        incident_blocked_lanes=spec.incident_blocked_lanes,
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
    source_inflows = project.metadata.get("ctm_source_inflows_veh_h", {}) or {}
    lane_delta_links = []
    for link in project.network.links.values():
        delta = link.metadata.get("lane_scenario_delta")
        if delta is not None:
            lane_delta_links.append(f"{link.id}:{delta}")

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
        "incident_model": incident.get("incident_model", ""),
        "incident_capacity_factor": incident.get("capacity_factor", ""),
        "configured_capacity_factor": incident.get("configured_capacity_factor", ""),
        "blocked_lanes": incident.get("blocked_lanes", ""),
        "lanes_total": incident.get("lanes_total", ""),
        "incident_start_sec": incident.get("start_time_sec", ""),
        "incident_end_sec": incident.get("end_time_sec", ""),
        "lane_delta_links": ";".join(sorted(lane_delta_links)),
        "source_inflow_total_veh_h": round(sum(float(v) for v in source_inflows.values()), 3),
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
    heatmap_project = (
        loaded.get("lane_blockage")
        or loaded.get("lane_blockage_added_lane")
        or next(iter(loaded.values()))
    )
    _plot_incident_heatmap(
        heatmap_project,
        incident_link_id,
        output_dir / "plot_incident_link_heatmap.png",
    )
    _plot_mass_balance(loaded, output_dir / "plot_mass_balance.png")


def _time_axis(length: int, snapshot_interval_sec: int) -> list[float]:
    return [i * snapshot_interval_sec / 60.0 for i in range(length)]


def _snapshot_interval(project) -> int:
    return int(project.metadata.get("ctm_scenario_config", {}).get("snapshot_interval_sec", 60))


def _incident_window(projects: dict[str, Any]) -> tuple[float, float] | None:
    for project in projects.values():
        incident = project.metadata.get("ctm_incident", {}) or {}
        if "start_time_sec" in incident and "end_time_sec" in incident:
            return float(incident["start_time_sec"]) / 60.0, float(incident["end_time_sec"]) / 60.0
    return None


def _shade_incident_window(projects: dict[str, Any]) -> None:
    window = _incident_window(projects)
    if not window:
        return
    start_min, end_min = window
    plt.axvspan(start_min, end_min, alpha=0.12, label="incident window")


def _density_reference_lines(project, link_id: str) -> None:
    link = project.network.links.get(link_id)
    if link is None:
        return
    config = project.metadata.get("ctm_scenario_config", {}) or {}
    highway_params = config.get("highway_params", {}) or {}
    highway = link.metadata.get("highway", "default")
    params = highway_params.get(highway, highway_params.get("default", {})) or {}
    lanes = float(link.parameters.get("lanes_total", 1) or 1)
    jam_per_lane = float(config.get("jam_density_pcu_km_per_lane", 140.0))
    jam_density = jam_per_lane * lanes
    speed = float(params.get("speed_kph", 0.0) or 0.0)
    capacity_per_lane = float(params.get("cap_per_lane", 0.0) or 0.0)
    if speed > 0.0 and capacity_per_lane > 0.0:
        critical_density = capacity_per_lane * lanes / speed
        plt.axhline(critical_density, linestyle="--", linewidth=1, label="critical density")
    plt.axhline(jam_density, linestyle=":", linewidth=1, label="jam density")


def _same_incident_lanes(projects: dict[str, Any], link_id: str) -> bool:
    lanes = set()
    for project in projects.values():
        link = project.network.links.get(link_id)
        if link is not None:
            lanes.add(int(link.parameters.get("lanes_total", 1) or 1))
    return len(lanes) <= 1


def _plot_incident_density(projects: dict[str, Any], link_id: str, path: Path) -> None:
    plt.figure()
    for name, project in projects.items():
        link = project.network.links.get(link_id)
        if link is None:
            continue
        history = link.results.get("history_cells_density_pcu_km", []) or []
        series = [sum(snapshot) / len(snapshot) if snapshot else 0.0 for snapshot in history]
        plt.plot(_time_axis(len(series), _snapshot_interval(project)), series, label=name)
    first_project = next(iter(projects.values()))
    if _same_incident_lanes(projects, link_id):
        _density_reference_lines(first_project, link_id)
    _shade_incident_window(projects)
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
        plt.plot(_time_axis(len(series), _snapshot_interval(project)), series, label=name)
    _shade_incident_window(projects)
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
        plt.plot(_time_axis(len(total_queue), _snapshot_interval(project)), total_queue, label=name)
    _shade_incident_window(projects)
    plt.xlabel("Время, мин")
    plt.ylabel("Внешняя очередь источников, pcu")
    plt.title("Накопление неудовлетворенного входного спроса")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def _plot_incident_heatmap(project, link_id: str, path: Path) -> None:
    link = project.network.links.get(link_id)
    if link is None:
        return
    history = link.results.get("history_cells_density_pcu_km", []) or []
    if not history:
        return
    # matrix: rows are cells, columns are time snapshots.
    matrix = [list(row) for row in zip(*history)]
    duration_min = (len(history) - 1) * _snapshot_interval(project) / 60.0
    plt.figure()
    plt.imshow(
        matrix,
        aspect="auto",
        origin="lower",
        extent=[0.0, max(duration_min, 0.001), 0, len(matrix)],
    )
    incident = project.metadata.get("ctm_incident", {}) or {}
    if "start_time_sec" in incident and "end_time_sec" in incident:
        plt.axvline(float(incident["start_time_sec"]) / 60.0, linestyle="--", linewidth=1)
        plt.axvline(float(incident["end_time_sec"]) / 60.0, linestyle="--", linewidth=1)
    plt.colorbar(label="Плотность, pcu/km")
    plt.xlabel("Время, мин")
    plt.ylabel("Индекс CTM-ячейки на link")
    plt.title(f"Пространственно-временная диаграмма плотности: {link_id}")
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def _plot_mass_balance(projects: dict[str, Any], path: Path) -> None:
    names = []
    balance_errors = []
    demand_errors = []
    for name, project in projects.items():
        sim = project.metadata.get("ctm_simulation", {}) or {}
        names.append(name)
        balance_errors.append(abs(float(sim.get("conservation_error_pcu", 0.0) or 0.0)))
        demand_errors.append(abs(float(sim.get("demand_balance_error_pcu", 0.0) or 0.0)))
    x = list(range(len(names)))
    width = 0.35
    plt.figure()
    plt.bar([v - width / 2 for v in x], balance_errors, width, label="network balance")
    plt.bar([v + width / 2 for v in x], demand_errors, width, label="full demand balance")
    plt.xticks(x, names, rotation=20)
    plt.ylabel("Абсолютная ошибка, pcu")
    plt.title("Проверка закона сохранения")
    plt.legend()
    plt.grid(True, axis="y")
    plt.tight_layout()
    plt.savefig(path)
    plt.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run final baseline/lane-blockage CTM experiments.")
    parser.add_argument("--project", default="osm_network_project_map_nstu.json")
    parser.add_argument("--output-dir", default="ctm_experiments")
    parser.add_argument("--dt", type=float, default=0.5)
    parser.add_argument("--minutes", type=int, default=100)
    parser.add_argument("--snapshot-sec", type=int, default=10)
    parser.add_argument("--cell-length", type=float, default=15.0)
    parser.add_argument("--inflow", type=float, default=475.0)
    parser.add_argument("--incident-link", default=None)
    parser.add_argument("--incident-start", type=float, default=300.0)
    parser.add_argument("--incident-end", type=float, default=900.0)
    parser.add_argument("--incident-capacity-factor", type=float, default=0.1)
    parser.add_argument("--incident-speed-factor", type=float, default=1.0)
    parser.add_argument("--incident-blocked-lanes", type=int, default=1)
    parser.add_argument("--added-lane-delta", type=int, default=1)
    parser.add_argument("--fifo-strength", type=float, default=1.0)
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
        incident_blocked_lanes=None,
        fifo_strength=args.fifo_strength,
    )
    incident_link_id = args.incident_link or choose_default_incident_link(args.project, base_config)
    if incident_link_id is None:
        raise ValueError("could not choose an incident/control link")
    print(f"Incident/control link: {incident_link_id}")

    specs = [
        ExperimentSpec(
            name="baseline",
            fifo_strength=args.fifo_strength,
            incident_capacity_factor=1.0,
            incident_speed_factor=args.incident_speed_factor,
            incident_blocked_lanes=None,
        ),
        ExperimentSpec(
            name="lane_blockage",
            fifo_strength=args.fifo_strength,
            incident_capacity_factor=1.0,
            incident_speed_factor=args.incident_speed_factor,
            incident_blocked_lanes=args.incident_blocked_lanes,
        ),
        ExperimentSpec(
            name="lane_blockage_added_lane",
            fifo_strength=args.fifo_strength,
            incident_capacity_factor=1.0,
            incident_speed_factor=args.incident_speed_factor,
            incident_blocked_lanes=args.incident_blocked_lanes,
            lane_delta_by_link={incident_link_id: args.added_lane_delta},
        ),
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
