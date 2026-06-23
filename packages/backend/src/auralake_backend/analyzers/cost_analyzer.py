"""Platform-level (DBU) cost analysis — DB-first."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from sqlmodel import func, select

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer
from auralake_backend.db.models import EnrichedBillingResource


class CostAnalyzer(AbstractAnalyzer):
    name = "cost"

    def analyze(self) -> AnalysisResult:
        if self.session is not None:
            return self._analyze_from_db()
        return self._analyze_from_api()

    def _analyze_from_db(self) -> AnalysisResult:
        days = self.context.config.defaults.lookback_days
        end = date.today()
        start = end - timedelta(days=days)
        basis = self.pricing_basis()
        recommendations = []

        # Query enriched billing grouped by SKU
        rows = self.session.exec(  # type: ignore[union-attr]
            select(
                EnrichedBillingResource.sku,
                func.sum(EnrichedBillingResource.cost_usd),
                func.sum(EnrichedBillingResource.dbu_usage),
            )
            .where(
                EnrichedBillingResource.usage_date >= start,
                EnrichedBillingResource.usage_date <= end,
            )
            .group_by(EnrichedBillingResource.sku)
            .order_by(func.sum(EnrichedBillingResource.cost_usd).desc())
        ).all()

        total_cost = sum(float(r[1] or 0) for r in rows)
        by_sku = {r[0]: float(r[1] or 0) for r in rows}

        if self.rule_enabled("cost_high_sku"):
            threshold = self.rule_threshold("cost_high_sku", "min_cost_usd", 1000)
            for sku, cost in by_sku.items():
                if cost > float(threshold):
                    recommendations.append(
                        Recommendation(
                            type="cost_high_sku",
                            risk_level=RiskLevel.LOW,
                            resource_id=sku,
                            resource_name=sku,
                            title=f"High cost SKU: {sku}",
                            description=f"SKU {sku} cost ${cost:.2f} over {days} days.",
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
                "total_cost_usd": str(total_cost),
                "period_start": str(start),
                "period_end": str(end),
                "sku_count": len(by_sku),
            },
        )

    def _analyze_from_api(self) -> AnalysisResult:
        """Fallback: analyze from provider API (legacy path)."""
        cost_client = self.context.provider.get_cost_client()
        days = self.context.config.defaults.lookback_days
        end = date.today()
        start = end - timedelta(days=days)

        breakdown = cost_client.get_cost_breakdown(start, end)
        recommendations = []
        basis = self.pricing_basis()

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
                            description=f"SKU {sku} cost ${cost:.2f} over {days} days.",
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
