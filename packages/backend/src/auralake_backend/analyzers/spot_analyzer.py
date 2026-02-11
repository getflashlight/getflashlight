"""Spot instance adoption analysis."""

from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer


class SpotAnalyzer(AbstractAnalyzer):
    name = "spot"

    def analyze(self) -> AnalysisResult:
        compute = self.context.provider.get_compute_client()
        clusters = compute.list_clusters()
        min_savings = self.context.config.thresholds.spot_optimization.min_savings_pct
        recommendations = []
        basis = self.pricing_basis()

        if not self.rule_enabled("spot_eligible"):
            return AnalysisResult(
                analyzer_name=self.name,
                provider=self.context.config.provider,
                recommendations=[],
                summary={"clusters_analyzed": len(clusters), "spot_eligible": 0},
            )

        for cluster in clusters:
            if cluster.spot_enabled or cluster.num_workers == 0:
                continue
            util = compute.get_utilization(cluster.cluster_id)
            estimated_savings_pct = 60  # Typical spot savings
            if estimated_savings_pct >= min_savings:
                monthly_savings = (
                    Decimal(str(util.total_cost_usd * 0.6))
                    if util.total_cost_usd
                    else Decimal("200")
                )
                recommendations.append(
                    Recommendation(
                        type="spot_eligible",
                        risk_level=RiskLevel.MEDIUM,
                        resource_id=cluster.cluster_id,
                        resource_name=cluster.cluster_name,
                        title=f"Enable spot instances on '{cluster.cluster_name}'",
                        description=(
                            f"Cluster runs {cluster.num_workers}"
                            f" on-demand workers."
                            f" Spot can save ~{estimated_savings_pct}%."
                        ),
                        current_state={"spot_enabled": False, "num_workers": cluster.num_workers},
                        recommended_state={"spot_enabled": True, "spot_fallback": True},
                        estimated_monthly_savings_usd=monthly_savings,
                        savings_confidence=SavingsConfidence.MEDIUM,
                        pricing_basis=basis,
                    )
                )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={"clusters_analyzed": len(clusters), "spot_eligible": len(recommendations)},
        )
