"""Reserved Instance / Savings Plan recommendation analyzer."""

from __future__ import annotations

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer


class RIAnalyzer(AbstractAnalyzer):
    name = "ri_savings_plan"

    def analyze(self) -> AnalysisResult:
        infra_client = self.context.provider.get_infra_cost_client()
        ri_recs = infra_client.get_ri_savings_plan_recommendations()

        recommendations = []
        for rec in ri_recs:
            recommendations.append(
                Recommendation(
                    type="ri_savings_plan",
                    risk_level=RiskLevel.LOW,
                    resource_id=rec.resource_id,
                    resource_name=rec.resource_id,
                    title=(f"Consider {rec.recommended_commitment} for {rec.resource_id}"),
                    description=(
                        f"Current cost: ${rec.current_cost_usd:.2f}/hr. "
                        f"Savings: ${rec.estimated_savings_usd:.2f}/mo. "
                        f"Breakeven: {rec.breakeven_months} months."
                    ),
                    estimated_monthly_savings_usd=rec.estimated_savings_usd,
                    savings_confidence=SavingsConfidence.MEDIUM,
                    evidence={
                        "current_cost_usd": str(rec.current_cost_usd),
                        "commitment_type": rec.recommended_commitment,
                        "breakeven_months": rec.breakeven_months,
                    },
                )
            )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={"ri_recommendations": len(recommendations)},
        )
