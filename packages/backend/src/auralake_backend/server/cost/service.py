from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import AnalysisResult


class CostService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def get_report(self) -> AnalysisResult:
        from auralake_backend.analyzers.cost_analyzer import CostAnalyzer

        return CostAnalyzer(self.context).analyze()

    def get_breakdown(self, days: int = 30) -> dict:
        from datetime import date, timedelta

        cost_client = self.context.provider.get_cost_client()
        end = date.today()
        start = end - timedelta(days=days)
        return cost_client.get_cost_breakdown(start, end).model_dump()

    def get_tco(self) -> AnalysisResult:
        from auralake_backend.analyzers.tco_analyzer import TCOAnalyzer

        return TCOAnalyzer(self.context).analyze()

    def get_infra(self) -> AnalysisResult:
        from auralake_backend.analyzers.infra_cost_analyzer import InfraCostAnalyzer

        return InfraCostAnalyzer(self.context).analyze()
