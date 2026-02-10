"""Delta Lake table maintenance analysis."""

from __future__ import annotations

from decimal import Decimal

from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)

from auralake_backend.analyzers.base import AbstractAnalyzer


class DeltaAnalyzer(AbstractAnalyzer):
    name = "delta"

    def analyze(self) -> AnalysisResult:
        recommendations = []

        # Note: table listing requires catalog/schema which depends on workspace config
        # This analyzer works best when called with specific tables
        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={"tables_analyzed": 0, "recommendations_count": 0},
        )

    def analyze_table(self, table_name: str) -> list[Recommendation]:
        """Analyze a specific table for maintenance needs."""
        storage = self.context.provider.get_storage_client()
        thresholds = self.context.config.thresholds.delta_maintenance
        recommendations = []

        try:
            stats = storage.get_table_stats(table_name)
            size_gb = float(stats.get("sizeInBytes", 0)) / (1024**3)
            num_files = int(stats.get("numFiles", 0))
            avg_file_mb = (size_gb * 1024) / num_files if num_files > 0 else 0

            if (
                avg_file_mb < thresholds.small_file_threshold_mb
                and size_gb >= thresholds.optimize_threshold_gb
            ):
                recommendations.append(
                    Recommendation(
                        type="delta_small_files",
                        risk_level=RiskLevel.LOW,
                        resource_id=table_name,
                        resource_name=table_name,
                        title=f"OPTIMIZE table '{table_name}' (small files)",
                        description=(
                            f"Table has {num_files} files averaging"
                            f" {avg_file_mb:.1f} MB. Consider running OPTIMIZE."
                        ),
                        current_state={
                            "num_files": num_files,
                            "avg_file_mb": avg_file_mb,
                            "size_gb": size_gb,
                        },
                        recommended_state={"action": "OPTIMIZE"},
                        estimated_monthly_savings_usd=Decimal("10"),
                        savings_confidence=SavingsConfidence.MEDIUM,
                    )
                )
        except Exception:
            pass

        return recommendations
