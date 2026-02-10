"""Cluster policy compliance analysis."""

from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer


class PolicyAnalyzer(AbstractAnalyzer):
    name = "policy"

    def analyze(self) -> AnalysisResult:
        compute = self.context.provider.get_compute_client()
        clusters = compute.list_clusters()
        recommendations = []

        # Check autotermination policy
        for cluster in clusters:
            if cluster.autotermination_minutes is None or cluster.autotermination_minutes == 0:
                recommendations.append(
                    Recommendation(
                        type="policy_no_autotermination",
                        risk_level=RiskLevel.LOW,
                        resource_id=cluster.cluster_id,
                        resource_name=cluster.cluster_name,
                        title=f"No autotermination policy on '{cluster.cluster_name}'",
                        description="Cluster lacks autotermination. This violates best practices.",
                        current_state={"autotermination_minutes": cluster.autotermination_minutes},
                        recommended_state={"autotermination_minutes": 60},
                        estimated_monthly_savings_usd=Decimal("50"),
                        savings_confidence=SavingsConfidence.LOW,
                    )
                )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={"clusters_audited": len(clusters), "violations": len(recommendations)},
        )
