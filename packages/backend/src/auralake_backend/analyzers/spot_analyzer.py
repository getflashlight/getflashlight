"""Spot instance adoption analysis — DB-first."""

from __future__ import annotations

from decimal import Decimal

from sqlmodel import func, select

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer
from auralake_backend.db.models import ComputeResourceRecord, EnrichedBillingResource


class SpotAnalyzer(AbstractAnalyzer):
    name = "spot"

    def analyze(self) -> AnalysisResult:
        if self.session is not None:
            return self._analyze_from_db()
        return self._analyze_from_api()

    def _analyze_from_db(self) -> AnalysisResult:
        min_savings = self.context.config.thresholds.spot_optimization.min_savings_pct
        basis = self.pricing_basis()

        if not self.rule_enabled("spot_eligible"):
            return AnalysisResult(
                analyzer_name=self.name,
                provider=self.context.config.provider,
                recommendations=[],
                summary={"clusters_analyzed": 0, "spot_eligible": 0},
            )

        # Query clusters from DB
        clusters = self.session.exec(  # type: ignore[union-attr]
            select(ComputeResourceRecord).where(
                ComputeResourceRecord.resource_type.in_(  # type: ignore[union-attr]
                    ["all_purpose_cluster", "job_cluster"]
                ),
            )
        ).all()

        # Get billing cost per cluster for savings estimate
        billing_rows = self.session.exec(  # type: ignore[union-attr]
            select(
                EnrichedBillingResource.resource_id,
                func.sum(EnrichedBillingResource.cost_usd),
            )
            .where(
                EnrichedBillingResource.resource_type == "cluster",
            )
            .group_by(EnrichedBillingResource.resource_id)
        ).all()
        cost_by_cluster = {r[0]: float(r[1] or 0) for r in billing_rows}

        recommendations = []
        for cluster in clusters:
            if cluster.spot_enabled or (cluster.num_workers or 0) == 0:
                continue

            total_cost = cost_by_cluster.get(cluster.resource_id, 0)
            estimated_savings_pct = 60
            if estimated_savings_pct >= min_savings:
                monthly_savings = (
                    Decimal(str(total_cost * 0.6)) if total_cost else Decimal("200")
                )
                recommendations.append(
                    Recommendation(
                        type="spot_eligible",
                        risk_level=RiskLevel.MEDIUM,
                        resource_id=cluster.resource_id,
                        resource_name=cluster.resource_name,
                        title=f"Enable spot instances on '{cluster.resource_name}'",
                        description=(
                            f"Cluster runs {cluster.num_workers}"
                            f" on-demand workers."
                            f" Spot can save ~{estimated_savings_pct}%."
                        ),
                        current_state={
                            "spot_enabled": False,
                            "num_workers": cluster.num_workers,
                        },
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
            summary={
                "clusters_analyzed": len(clusters),
                "spot_eligible": len(recommendations),
            },
        )

    def _analyze_from_api(self) -> AnalysisResult:
        """Fallback: analyze from provider API (legacy path)."""
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
            estimated_savings_pct = 60
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
                        current_state={
                            "spot_enabled": False,
                            "num_workers": cluster.num_workers,
                        },
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
            summary={
                "clusters_analyzed": len(clusters),
                "spot_eligible": len(recommendations),
            },
        )
