from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import (
    ActionResult,
    AnalysisResult,
    Recommendation,
    RiskLevel,
)


class DeltaService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def scan(self) -> AnalysisResult:
        from auralake_backend.analyzers.delta_analyzer import DeltaAnalyzer

        return DeltaAnalyzer(self.context).analyze()

    def optimize(self, table: str) -> ActionResult:
        from auralake_backend.actions.delta_actions import OptimizeTableAction

        rec = Recommendation(
            type="delta_optimize",
            risk_level=RiskLevel.LOW,
            resource_id=table,
            resource_name=table,
            title=f"Optimize Delta table {table}",
            description=f"Run OPTIMIZE on {table} to compact small files.",
        )
        action = OptimizeTableAction(self.context)
        return action.execute(rec)

    def vacuum(self, table: str, retention_hours: int = 168) -> ActionResult:
        from auralake_backend.actions.delta_actions import VacuumTableAction

        rec = Recommendation(
            type="delta_vacuum",
            risk_level=RiskLevel.MEDIUM,
            resource_id=table,
            resource_name=table,
            title=f"Vacuum Delta table {table}",
            description=(f"Run VACUUM on {table} with {retention_hours}h retention."),
            recommended_state={"retention_hours": retention_hours},
        )
        action = VacuumTableAction(self.context)
        return action.execute(rec)
