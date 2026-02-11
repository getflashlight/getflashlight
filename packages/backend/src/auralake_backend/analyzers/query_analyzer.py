"""Query analysis — detects expensive queries and plan anti-patterns."""

from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.agent.plan_parser import PlanParser
from auralake_backend.analyzers.base import AbstractAnalyzer


class QueryAnalyzer(AbstractAnalyzer):
    name = "query"

    def analyze(self) -> AnalysisResult:
        query_client = self.context.provider.get_query_client()
        parser = PlanParser()
        days = self.context.config.defaults.lookback_days
        basis = self.pricing_basis()

        queries = query_client.get_query_history(hours=days * 24, limit=500)
        recommendations = []

        # Sort by duration to find expensive queries
        sorted_queries = sorted(queries, key=lambda q: q.get("duration_ms", 0) or 0, reverse=True)

        # Top expensive queries
        if self.rule_enabled("query_expensive"):
            min_duration_ms = int(
                self.rule_threshold("query_expensive", "min_duration_ms", 300_000)
            )
            for query in sorted_queries[:10]:
                duration_ms = query.get("duration_ms", 0) or 0
                if duration_ms > min_duration_ms:
                    recommendations.append(
                        Recommendation(
                            type="query_expensive",
                            risk_level=RiskLevel.LOW,
                            resource_id=query.get("query_id", ""),
                            resource_name=f"Query by {query.get('user_name', 'unknown')}",
                            title=f"Expensive query ({duration_ms / 1000:.0f}s)",
                            description=(query.get("query_text", "")[:200] + "...")
                            if len(query.get("query_text", "")) > 200
                            else query.get("query_text", ""),
                            estimated_monthly_savings_usd=Decimal("0"),
                            savings_confidence=SavingsConfidence.LOW,
                            pricing_basis=basis,
                            evidence={
                                "duration_ms": duration_ms,
                                "user_name": query.get("user_name"),
                                "rows_produced": query.get("rows_produced"),
                            },
                        )
                    )

        # Anti-pattern analysis on expensive queries
        if self.rule_enabled("query_anti_pattern"):
            for query in sorted_queries[:20]:
                query_text = query.get("query_text", "")
                query_id = query.get("query_id", "")
                if not query_text:
                    continue

                try:
                    plan_text = query_client.explain_query(query_text)
                    if plan_text:
                        plan = parser.parse(query_id, plan_text, query_text)
                        for ap in plan.anti_patterns:
                            recommendations.append(
                                Recommendation(
                                    type=f"query_{ap.type}",
                                    risk_level=RiskLevel.MEDIUM
                                    if ap.severity == "high"
                                    else RiskLevel.LOW,
                                    resource_id=query_id,
                                    resource_name=f"Query: {query_text[:60]}...",
                                    title=f"Query anti-pattern: {ap.type}",
                                    description=ap.description,
                                    estimated_monthly_savings_usd=Decimal("0"),
                                    savings_confidence=SavingsConfidence.LOW,
                                    pricing_basis=basis,
                                    evidence={
                                        "anti_pattern": ap.type,
                                        "recommendation": ap.recommendation,
                                    },
                                )
                            )
                except Exception:
                    continue

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "total_queries_analyzed": len(queries),
                "expensive_queries": len(
                    [r for r in recommendations if r.type == "query_expensive"]
                ),
                "anti_patterns_found": len(
                    [
                        r
                        for r in recommendations
                        if r.type.startswith("query_") and r.type != "query_expensive"
                    ]
                ),
            },
        )
