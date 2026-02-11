"""Delta Lake table maintenance analysis.

Reads pre-collected table metadata and maintenance history from the database
(populated by the catalog_tables ETL worker) and generates recommendations
for 6 maintenance scenarios.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import structlog
from auralake_shared.models.recommendations import (
    AnalysisResult,
    Recommendation,
    RiskLevel,
    SavingsConfidence,
)
from sqlmodel import Session, select

from auralake_backend.analyzers.base import AbstractAnalyzer
from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import UnityCatalogTableRecord

logger = structlog.get_logger(__name__)


class DeltaAnalyzer(AbstractAnalyzer):
    name = "delta"

    def analyze(self) -> AnalysisResult:
        """Analyze all Delta tables from the DB for maintenance needs."""
        thresholds = self.context.config.thresholds.delta_maintenance
        recommendations: list[Recommendation] = []
        tables_analyzed = 0

        with Session(get_engine()) as session:
            tables = session.exec(
                select(UnityCatalogTableRecord).where(
                    UnityCatalogTableRecord.data_format.in_(  # type: ignore[union-attr]
                        ["DELTA", "delta", None]
                    )
                )
            ).all()

            for table in tables:
                try:
                    recs = self._analyze_table(table, thresholds)
                    recommendations.extend(recs)
                    tables_analyzed += 1
                except Exception as exc:
                    logger.warning(
                        "delta_table_analysis_failed",
                        table=table.full_name,
                        error=str(exc),
                    )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "tables_from_db": len(tables),
                "tables_analyzed": tables_analyzed,
                "recommendations_count": len(recommendations),
            },
        )

    def _analyze_table(
        self, table: UnityCatalogTableRecord, thresholds: object
    ) -> list[Recommendation]:
        """Generate recommendations for a single table."""
        recommendations: list[Recommendation] = []
        size_gb = (table.size_bytes or 0) / (1024**3)
        num_files = table.num_files or 0
        avg_file_mb = (size_gb * 1024) / num_files if num_files > 0 else 0
        now = datetime.now(UTC)

        # 1. Small files
        if (
            avg_file_mb > 0
            and avg_file_mb < thresholds.small_file_threshold_mb  # type: ignore[attr-defined]
            and size_gb >= thresholds.optimize_threshold_gb  # type: ignore[attr-defined]
        ):
            savings = max(Decimal(str(size_gb * 0.023 * 0.5)), Decimal("5"))
            recommendations.append(
                Recommendation(
                    type="delta_small_files",
                    risk_level=RiskLevel.LOW,
                    resource_id=table.full_name,
                    resource_name=table.full_name,
                    title=f"OPTIMIZE table '{table.full_name}' (small files)",
                    description=(
                        f"Table has {num_files} files averaging"
                        f" {avg_file_mb:.1f} MB. Consider running OPTIMIZE."
                    ),
                    current_state={
                        "num_files": num_files,
                        "avg_file_mb": round(avg_file_mb, 1),
                        "size_gb": round(size_gb, 2),
                    },
                    recommended_state={"action": "OPTIMIZE"},
                    estimated_monthly_savings_usd=savings,
                    savings_confidence=SavingsConfidence.MEDIUM,
                )
            )

        # Skip history-based checks for tiny tables
        if size_gb < thresholds.min_table_size_gb_for_history:  # type: ignore[attr-defined]
            return recommendations

        # 2. Stale OPTIMIZE
        optimize_stale_days = thresholds.optimize_stale_days  # type: ignore[attr-defined]
        if table.last_optimized_at:
            days_since_optimize = (now - table.last_optimized_at.replace(tzinfo=UTC)).days
            if days_since_optimize > optimize_stale_days:
                savings = max(Decimal(str(size_gb * 0.023 * 0.3)), Decimal("5"))
                recommendations.append(
                    Recommendation(
                        type="delta_stale_optimize",
                        risk_level=RiskLevel.LOW,
                        resource_id=table.full_name,
                        resource_name=table.full_name,
                        title=(
                            f"Table '{table.full_name}' not optimized in {days_since_optimize} days"
                        ),
                        description=(
                            f"Last OPTIMIZE was {days_since_optimize} days ago"
                            f" (threshold: {optimize_stale_days} days)."
                            f" Table size: {size_gb:.1f} GB."
                        ),
                        current_state={
                            "last_optimized_at": table.last_optimized_at.isoformat(),
                            "days_since_optimize": days_since_optimize,
                            "size_gb": round(size_gb, 2),
                        },
                        recommended_state={"action": "OPTIMIZE"},
                        estimated_monthly_savings_usd=savings,
                        savings_confidence=SavingsConfidence.LOW,
                    )
                )
        elif size_gb >= thresholds.optimize_threshold_gb and table.history_error is None:  # type: ignore[attr-defined]
            # Table large enough but never optimized (and history was fetched successfully)
            savings = max(Decimal(str(size_gb * 0.023 * 0.3)), Decimal("5"))
            recommendations.append(
                Recommendation(
                    type="delta_stale_optimize",
                    risk_level=RiskLevel.LOW,
                    resource_id=table.full_name,
                    resource_name=table.full_name,
                    title=f"Table '{table.full_name}' has never been optimized",
                    description=(f"No OPTIMIZE history found. Table size: {size_gb:.1f} GB."),
                    current_state={
                        "last_optimized_at": None,
                        "size_gb": round(size_gb, 2),
                    },
                    recommended_state={"action": "OPTIMIZE"},
                    estimated_monthly_savings_usd=savings,
                    savings_confidence=SavingsConfidence.LOW,
                )
            )

        # 3. Stale VACUUM
        vacuum_stale_days = thresholds.vacuum_stale_days  # type: ignore[attr-defined]
        if table.last_vacuumed_at:
            days_since_vacuum = (now - table.last_vacuumed_at.replace(tzinfo=UTC)).days
            if days_since_vacuum > vacuum_stale_days:
                savings = max(Decimal(str(size_gb * 0.1 * 0.023)), Decimal("1"))
                recommendations.append(
                    Recommendation(
                        type="delta_stale_vacuum",
                        risk_level=RiskLevel.LOW,
                        resource_id=table.full_name,
                        resource_name=table.full_name,
                        title=f"Table '{table.full_name}' not vacuumed in {days_since_vacuum} days",
                        description=(
                            f"Last VACUUM was {days_since_vacuum} days ago"
                            f" (threshold: {vacuum_stale_days} days)."
                            f" Stale files may waste ~10% of storage ({size_gb * 0.1:.1f} GB)."
                        ),
                        current_state={
                            "last_vacuumed_at": table.last_vacuumed_at.isoformat(),
                            "days_since_vacuum": days_since_vacuum,
                            "size_gb": round(size_gb, 2),
                        },
                        recommended_state={"action": "VACUUM"},
                        estimated_monthly_savings_usd=savings,
                        savings_confidence=SavingsConfidence.LOW,
                    )
                )
        elif size_gb >= thresholds.optimize_threshold_gb and table.history_error is None:  # type: ignore[attr-defined]
            savings = max(Decimal(str(size_gb * 0.1 * 0.023)), Decimal("1"))
            recommendations.append(
                Recommendation(
                    type="delta_stale_vacuum",
                    risk_level=RiskLevel.LOW,
                    resource_id=table.full_name,
                    resource_name=table.full_name,
                    title=f"Table '{table.full_name}' has never been vacuumed",
                    description=(
                        f"No VACUUM history found. Table size: {size_gb:.1f} GB."
                        f" Stale files may waste ~10% of storage."
                    ),
                    current_state={
                        "last_vacuumed_at": None,
                        "size_gb": round(size_gb, 2),
                    },
                    recommended_state={"action": "VACUUM"},
                    estimated_monthly_savings_usd=savings,
                    savings_confidence=SavingsConfidence.LOW,
                )
            )

        # 4. Over-optimized
        over_optimize_threshold = thresholds.over_optimize_threshold  # type: ignore[attr-defined]
        optimize_count = table.optimize_count_30d or 0
        if optimize_count > over_optimize_threshold:
            wasted_runs = optimize_count - over_optimize_threshold
            savings = Decimal(str(wasted_runs * 0.50))
            recommendations.append(
                Recommendation(
                    type="delta_over_optimized",
                    risk_level=RiskLevel.LOW,
                    resource_id=table.full_name,
                    resource_name=table.full_name,
                    title=f"Table '{table.full_name}' optimized {optimize_count} times in 30 days",
                    description=(
                        f"OPTIMIZE ran {optimize_count} times in the last 30 days"
                        f" (threshold: {over_optimize_threshold})."
                        f" Consider reducing frequency."
                    ),
                    current_state={
                        "optimize_count_30d": optimize_count,
                        "threshold": over_optimize_threshold,
                    },
                    recommended_state={"action": "reduce_optimize_frequency"},
                    estimated_monthly_savings_usd=savings,
                    savings_confidence=SavingsConfidence.MEDIUM,
                )
            )

        # 5. Migrate Z-ORDER to liquid clustering
        if table.uses_zordering and not table.uses_liquid_clustering:
            savings = max(Decimal(str(size_gb * 0.50 * 0.5)), Decimal("5"))
            recommendations.append(
                Recommendation(
                    type="delta_migrate_to_liquid_clustering",
                    risk_level=RiskLevel.MEDIUM,
                    resource_id=table.full_name,
                    resource_name=table.full_name,
                    title=f"Migrate '{table.full_name}' from Z-ORDER to liquid clustering",
                    description=(
                        f"Table uses Z-ORDER but not liquid clustering."
                        f" Liquid clustering avoids full data rewrites and"
                        f" provides incremental, automatic optimization."
                        f" Table size: {size_gb:.1f} GB."
                    ),
                    current_state={
                        "uses_zordering": True,
                        "uses_liquid_clustering": False,
                        "size_gb": round(size_gb, 2),
                    },
                    recommended_state={"action": "ALTER TABLE ... CLUSTER BY (...)"},
                    estimated_monthly_savings_usd=savings,
                    savings_confidence=SavingsConfidence.LOW,
                )
            )

        # 6. Enable clustering for large unpartitioned/unclustered tables
        has_partitioning = bool(table.partition_columns)
        has_clustering = bool(table.clustering_columns)
        if (
            size_gb >= 10
            and not has_partitioning
            and not has_clustering
            and not table.uses_zordering
            and not table.uses_liquid_clustering
        ):
            recommendations.append(
                Recommendation(
                    type="delta_enable_clustering",
                    risk_level=RiskLevel.MEDIUM,
                    resource_id=table.full_name,
                    resource_name=table.full_name,
                    title=f"Enable clustering for large table '{table.full_name}'",
                    description=(
                        f"Table is {size_gb:.1f} GB with no partitioning,"
                        f" clustering, or Z-ORDER. Liquid clustering can"
                        f" significantly improve query performance."
                    ),
                    current_state={
                        "size_gb": round(size_gb, 2),
                        "partition_columns": [],
                        "clustering_columns": [],
                        "uses_zordering": False,
                        "uses_liquid_clustering": False,
                    },
                    recommended_state={"action": "ALTER TABLE ... CLUSTER BY (...)"},
                    estimated_monthly_savings_usd=Decimal("0"),
                    savings_confidence=SavingsConfidence.LOW,
                )
            )

        return recommendations
