from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult


class ResourceService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def scan(self) -> AnalysisResult:
        from auralake_backend.analyzers.idle_resource_analyzer import IdleResourceAnalyzer

        return IdleResourceAnalyzer(self.context).analyze()

    def report(self) -> AnalysisResult:
        from auralake_backend.analyzers.idle_resource_analyzer import IdleResourceAnalyzer

        return IdleResourceAnalyzer(self.context).analyze()

    def cleanup(self, resource_type: str | None = None) -> list[ActionResult]:
        from auralake_backend.actions.resource_actions import TerminateIdleClusterAction
        from auralake_backend.analyzers.idle_resource_analyzer import IdleResourceAnalyzer

        result = IdleResourceAnalyzer(self.context).analyze()
        action = TerminateIdleClusterAction(self.context)
        action_results: list[ActionResult] = []
        for rec in result.recommendations:
            if resource_type and rec.type != resource_type:
                continue
            action_results.append(action.execute(rec))
        return action_results
