"""AWS infrastructure cost analysis."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from auralake.analyzers.base import AbstractAnalyzer
from auralake.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)


class InfraCostAnalyzer(AbstractAnalyzer):
    name = "infra_cost"

    def analyze(self) -> AnalysisResult:
        infra_client = self.context.provider.get_infra_cost_client()
        days = self.context.config.defaults.lookback_days
        end = date.today()
        start = end - timedelta(days=days)

        compute_costs = infra_client.get_compute_costs(start, end)
        storage_costs = infra_client.get_storage_costs(start, end)
        transfer_costs = infra_client.get_data_transfer_costs(start, end)

        total_compute = sum(
            (c.cost_usd for c in compute_costs), Decimal("0")
        )
        total_storage = sum(
            (s.cost_usd for s in storage_costs), Decimal("0")
        )
        total_transfer = sum(
            (t.cost_usd for t in transfer_costs), Decimal("0")
        )

        recommendations = []

        # Flag high data transfer costs
        if total_transfer > Decimal("500"):
            recommendations.append(
                Recommendation(
                    type="infra_high_transfer",
                    risk_level=RiskLevel.MEDIUM,
                    resource_id="data_transfer",
                    resource_name="AWS Data Transfer",
                    title="High data transfer costs detected",
                    description=(
                        f"Data transfer costs of ${total_transfer:.2f} "
                        f"over {days} days. Consider VPC endpoints or "
                        f"S3 gateway."
                    ),
                    estimated_monthly_savings_usd=(
                        total_transfer * Decimal("0.3")
                    ),
                    savings_confidence=SavingsConfidence.LOW,
                    evidence={
                        "total_transfer_usd": str(total_transfer),
                    },
                )
            )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "total_compute_usd": str(total_compute),
                "total_storage_usd": str(total_storage),
                "total_transfer_usd": str(total_transfer),
                "total_infra_usd": str(
                    total_compute + total_storage + total_transfer
                ),
            },
        )
