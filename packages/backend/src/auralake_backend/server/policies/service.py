from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult


class PolicyService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def audit(self) -> AnalysisResult:
        from auralake_backend.analyzers.policy_analyzer import PolicyAnalyzer

        return PolicyAnalyzer(self.context).analyze()

    def recommend(self) -> AnalysisResult:
        from auralake_backend.analyzers.policy_analyzer import PolicyAnalyzer

        return PolicyAnalyzer(self.context).analyze()

    def apply(self) -> list[ActionResult]:
        from auralake_backend.actions.policy_actions import SetAutotermination
        from auralake_backend.analyzers.policy_analyzer import PolicyAnalyzer

        result = PolicyAnalyzer(self.context).analyze()
        action = SetAutotermination(self.context)
        action_results: list[ActionResult] = []
        for rec in result.recommendations:
            if rec.type == "policy_no_autotermination":
                action_results.append(action.execute(rec))
        return action_results
