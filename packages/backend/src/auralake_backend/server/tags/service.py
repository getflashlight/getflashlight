from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult


class TagService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def scan(self) -> AnalysisResult:
        from auralake_backend.analyzers.tag_analyzer import TagAnalyzer

        return TagAnalyzer(self.context).analyze()

    def report(self) -> AnalysisResult:
        from auralake_backend.analyzers.tag_analyzer import TagAnalyzer

        return TagAnalyzer(self.context).analyze()

    def enforce(self) -> list[ActionResult]:
        from auralake_backend.actions.tag_actions import EnforceTagsAction
        from auralake_backend.analyzers.tag_analyzer import TagAnalyzer

        result = TagAnalyzer(self.context).analyze()
        action = EnforceTagsAction(self.context)
        action_results: list[ActionResult] = []
        for rec in result.recommendations:
            action_results.append(action.execute(rec))
        return action_results
