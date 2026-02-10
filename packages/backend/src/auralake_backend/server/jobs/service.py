from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult


class JobService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def analyze(self) -> AnalysisResult:
        from auralake_backend.analyzers.job_analyzer import JobAnalyzer

        return JobAnalyzer(self.context).analyze()

    def stale(self) -> AnalysisResult:
        from auralake_backend.analyzers.job_analyzer import JobAnalyzer

        result = JobAnalyzer(self.context).analyze()
        result.recommendations = [r for r in result.recommendations if r.type == "job_stale"]
        return result

    def recommend(self) -> AnalysisResult:
        from auralake_backend.analyzers.job_analyzer import JobAnalyzer

        return JobAnalyzer(self.context).analyze()

    def consolidate(self) -> list[ActionResult]:
        from auralake_backend.actions.job_actions import ConsolidateJobsAction
        from auralake_backend.analyzers.job_analyzer import JobAnalyzer

        result = JobAnalyzer(self.context).analyze()
        action = ConsolidateJobsAction(self.context)
        action_results: list[ActionResult] = []
        for rec in result.recommendations:
            if rec.type == "job_consolidation":
                action_results.append(action.execute(rec))
        return action_results
