from __future__ import annotations

import math

from models import Project


class CalibrationDiagnosticsService:
    def build_diagnostics(self, project: Project) -> list[dict]:
        diagnostics: list[dict] = []
        for link in project.network.links.values():
            if not link.observed_counts:
                continue

            observed_pcu_h = 0.0
            for vehicle_type, count in link.observed_counts.items():
                observed_pcu_h += float(count) * float(project.pcu_coefficients.get(vehicle_type, 1.0))

            simulated_pcu_h = float(link.results.get("avg_flow_pcu_h", 0.0))
            delta_pcu_h = simulated_pcu_h - observed_pcu_h
            delta_pct = (delta_pcu_h / observed_pcu_h * 100.0) if observed_pcu_h else 0.0
            diagnostics.append(
                {
                    "link_id": link.id,
                    "observed_pcu_h": round(observed_pcu_h, 2),
                    "simulated_pcu_h": round(simulated_pcu_h, 2),
                    "delta_pcu_h": round(delta_pcu_h, 2),
                    "delta_pct": round(delta_pct, 2),
                    "status": "review" if abs(delta_pct) > 15.0 else "ok",
                }
            )
        return diagnostics

    def build_detector_metrics(self, project: Project) -> dict:
        errors = []
        abs_pct_errors = []
        for link in project.network.links.values():
            if not link.observed_counts:
                continue

            observed_pcu_h = 0.0
            for vehicle_type, count in link.observed_counts.items():
                observed_pcu_h += float(count) * float(project.pcu_coefficients.get(vehicle_type, 1.0))
            if observed_pcu_h <= 0:
                continue

            simulated_pcu_h = float(link.results.get("avg_flow_pcu_h", 0.0))
            error = simulated_pcu_h - observed_pcu_h
            errors.append(error)
            abs_pct_errors.append(abs(error) / observed_pcu_h * 100.0)

        if not errors:
            return {
                "detector_count": 0,
                "rmse_pcu_h": 0.0,
                "mape_pct": 0.0,
                "bias_pcu_h": 0.0,
                "status": "no_observations",
            }

        rmse = math.sqrt(sum(error * error for error in errors) / len(errors))
        mape = sum(abs_pct_errors) / len(abs_pct_errors)
        bias = sum(errors) / len(errors)
        return {
            "detector_count": len(errors),
            "rmse_pcu_h": round(rmse, 2),
            "mape_pct": round(mape, 2),
            "bias_pcu_h": round(bias, 2),
            "status": "review" if mape > 15.0 else "ok",
        }
