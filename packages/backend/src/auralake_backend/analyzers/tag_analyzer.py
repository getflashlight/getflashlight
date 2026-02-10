"""Tag policy enforcement analysis."""

from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer


class TagAnalyzer(AbstractAnalyzer):
    name = "tag"

    def analyze(self) -> AnalysisResult:
        compute = self.context.provider.get_compute_client()
        clusters = compute.list_clusters()
        required_tags = [t.key for t in self.context.config.tag_policy.required_tags]
        recommendations = []

        for cluster in clusters:
            missing = [t for t in required_tags if t not in cluster.tags]
            if missing:
                recommendations.append(
                    Recommendation(
                        type="tag_missing",
                        risk_level=RiskLevel.LOW,
                        resource_id=cluster.cluster_id,
                        resource_name=cluster.cluster_name,
                        title=f"Missing tags on '{cluster.cluster_name}': {', '.join(missing)}",
                        description=f"Cluster is missing required tags: {', '.join(missing)}.",
                        current_state={"tags": cluster.tags},
                        recommended_state={"missing_tags": missing},
                        estimated_monthly_savings_usd=Decimal("0"),
                        savings_confidence=SavingsConfidence.HIGH,
                    )
                )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "clusters_scanned": len(clusters),
                "violations": len(recommendations),
                "required_tags": required_tags,
            },
        )
