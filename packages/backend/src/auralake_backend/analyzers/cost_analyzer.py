"""Platform-level (DBU) cost analysis."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer


class CostAnalyzer(AbstractAnalyzer):
    name = "cost"

    def analyze(self) -> AnalysisResult:
        cost_client = self.context.provider.get_cost_client()
        days = self.context.config.defaults.lookback_days
        end = date.today()
        start = end - timedelta(days=days)

        breakdown = cost_client.get_cost_breakdown(start, end)
        recommendations = []
        basis = self.pricing_basis()

        # Flag high-cost SKUs
        if self.rule_enabled("cost_high_sku"):
            threshold = self.rule_threshold("cost_high_sku", "min_cost_usd", 1000)
            for sku, cost in breakdown.by_sku.items():
                if cost > Decimal(str(threshold)):
                    recommendations.append(
                        Recommendation(
                            type="cost_high_sku",
                            risk_level=RiskLevel.LOW,
                            resource_id=sku,
                            resource_name=sku,
                            title=f"High cost SKU: {sku}",
                            description=(f"SKU {sku} cost ${cost:.2f} over {days} days."),
                            estimated_monthly_savings_usd=Decimal("0"),
                            savings_confidence=SavingsConfidence.LOW,
                            pricing_basis=basis,
                            evidence={
                                "cost_usd": str(cost),
                                "period_days": days,
                            },
                        )
                    )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "total_cost_usd": str(breakdown.total_cost_usd),
                "period_start": str(start),
                "period_end": str(end),
                "sku_count": len(breakdown.by_sku),
                "cluster_count": len(breakdown.by_cluster),
            },
        )
