from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import AnalysisResult


class QueryService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def analyze(self) -> AnalysisResult:
        from auralake_backend.analyzers.query_analyzer import QueryAnalyzer

        return QueryAnalyzer(self.context).analyze()

    def expensive(self, days: int = 30, top_n: int = 20) -> list[dict]:
        query_client = self.context.provider.get_query_client()
        queries = query_client.get_expensive_queries(days=days, top_n=top_n)
        return [q.model_dump() for q in queries]

    def plans(self, workspace: str | None = None) -> list[dict]:
        from auralake_backend.db.query_plan_repository import QueryPlanRepository

        repo = QueryPlanRepository(self.context.config)
        plans = repo.list_plans(workspace=workspace)
        return [p.model_dump() for p in plans]
