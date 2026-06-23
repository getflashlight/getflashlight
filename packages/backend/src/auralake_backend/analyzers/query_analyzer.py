"""Query analysis — detects expensive queries and plan anti-patterns — DB-first."""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import select

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.agent.plan_parser import PlanParser
from auralake_backend.analyzers.base import AbstractAnalyzer
from auralake_backend.db.models import EnrichedQuery


class QueryAnalyzer(AbstractAnalyzer):
    name = "query"

    def analyze(self) -> AnalysisResult:
        if self.session is not None:
            return self._analyze_from_db()
        return self._analyze_from_api()

    def _analyze_from_db(self) -> AnalysisResult:
        basis = self.pricing_basis()
        recommendations = []

        # Query enriched queries sorted by duration
        queries = self.session.exec(  # type: ignore[union-attr]
            select(EnrichedQuery)
            .order_by(EnrichedQuery.duration_ms.desc())  # type: ignore[union-attr]
            .limit(500)
        ).all()

        # Top expensive queries
        if self.rule_enabled("query_expensive"):
            min_duration_ms = int(
                self.rule_threshold("query_expensive", "min_duration_ms", 300_000)
            )
            for query in queries[:10]:
                duration_ms = query.duration_ms or 0
                if duration_ms > min_duration_ms:
                    recommendations.append(
                        Recommendation(
                            type="query_expensive",
                            risk_level=RiskLevel.LOW,
                            resource_id=query.query_id,
                            resource_name=f"Query by {query.user_name or 'unknown'}",
                            title=f"Expensive query ({duration_ms / 1000:.0f}s)",
                            description=(query.query_text[:200] + "...")
                            if query.query_text and len(query.query_text) > 200
                            else (query.query_text or ""),
                            estimated_monthly_savings_usd=Decimal("0"),
                            savings_confidence=SavingsConfidence.LOW,
                            pricing_basis=basis,
                            evidence={
                                "duration_ms": duration_ms,
                                "user_name": query.user_name,
                                "rows_produced": query.rows_produced,
                                "warehouse_name": query.warehouse_name,
                                "warehouse_type": query.warehouse_type,
                            },
                        )
                    )

        # Anti-pattern analysis from pre-joined data
        if self.rule_enabled("query_anti_pattern"):
            for query in queries[:20]:
                if not query.has_plan or not query.anti_patterns:
                    continue

                patterns = query.anti_patterns
                pattern_list = (
                    patterns.get("patterns", []) if isinstance(patterns, dict) else []
                )

                for ap in pattern_list:
                    if not isinstance(ap, dict):
                        continue
                    ap_type = ap.get("type", "unknown")
                    severity = ap.get("severity", "low")
                    recommendations.append(
                        Recommendation(
                            type=f"query_{ap_type}",
                            risk_level=RiskLevel.MEDIUM
                            if severity == "high"
                            else RiskLevel.LOW,
                            resource_id=query.query_id,
                            resource_name=f"Query: {(query.query_text or '')[:60]}...",
                            title=f"Query anti-pattern: {ap_type}",
                            description=ap.get("description", ""),
                            estimated_monthly_savings_usd=Decimal("0"),
                            savings_confidence=SavingsConfidence.LOW,
                            pricing_basis=basis,
                            evidence={
                                "anti_pattern": ap_type,
                                "recommendation": ap.get("recommendation", ""),
                            },
                        )
                    )

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

    def _analyze_from_api(self) -> AnalysisResult:
        """Fallback: analyze from provider API (legacy path)."""
        query_client = self.context.provider.get_query_client()
        parser = PlanParser()
        days = self.context.config.defaults.lookback_days
        basis = self.pricing_basis()

        queries = query_client.get_query_history(hours=days * 24, limit=500)
        recommendations = []

        sorted_queries = sorted(
            queries, key=lambda q: q.get("duration_ms", 0) or 0, reverse=True
        )

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
