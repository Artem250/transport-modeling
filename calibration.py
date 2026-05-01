from __future__ import annotations

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
