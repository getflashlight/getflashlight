from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult


class SpotService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def analyze(self) -> AnalysisResult:
        from auralake_backend.analyzers.spot_analyzer import SpotAnalyzer

        return SpotAnalyzer(self.context).analyze()

    def recommend(self) -> AnalysisResult:
        from auralake_backend.analyzers.spot_analyzer import SpotAnalyzer

        return SpotAnalyzer(self.context).analyze()

    def apply(self, cluster_id: str | None = None) -> list[ActionResult]:
        from auralake_backend.actions.spot_actions import EnableSpotAction
        from auralake_backend.analyzers.spot_analyzer import SpotAnalyzer

        result = SpotAnalyzer(self.context).analyze()
        action = EnableSpotAction(self.context)
        action_results: list[ActionResult] = []
        for rec in result.recommendations:
            if cluster_id and rec.resource_id != cluster_id:
                continue
            action_results.append(action.execute(rec))
        return action_results
