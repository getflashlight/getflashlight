"""S3 tag compliance analysis.

Matches S3 objects from inventory reports to known Delta tables and
generates recommendations for orphaned or untagged objects.

Uses DuckDB to join S3 inventory rows (already in Postgres) against
Delta table locations, then pushes match results back to Postgres.
"""

from __future__ import annotations

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
from auralake_backend.db.models import S3InventoryObject

logger = structlog.get_logger(__name__)

# Estimated S3 cost per GB/month for orphan savings estimate (Standard tier)
_S3_COST_PER_GB_MONTH = Decimal("0.023")


class S3TagAnalyzer(AbstractAnalyzer):
    """Match S3 inventory objects to Delta tables and flag compliance issues."""

    name = "s3_tags"

    def analyze(self) -> AnalysisResult:
        storage = self.context.provider.get_storage_client()
        recommendations: list[Recommendation] = []

        # 1. Build table-location map from Delta table metadata
        table_locations: dict[str, str] = {}
        try:
            tables = storage.discover_all_tables()
        except Exception as exc:
            logger.warning("s3_tag_table_discovery_failed", error=str(exc))
            tables = []

        for table_info in tables:
            full_name = table_info.get("full_name", "")
            if not full_name:
                continue
            try:
                stats = storage.get_table_stats(full_name)
                location = stats.get("location", "")
                if location.startswith("s3://"):
                    # Normalise: ensure trailing slash for prefix matching
                    loc = location.rstrip("/") + "/"
                    table_locations[loc] = full_name
            except Exception:
                pass

        if not table_locations:
            return AnalysisResult(
                analyzer_name=self.name,
                provider=self.context.config.provider,
                recommendations=[],
                summary={"error": "no_delta_table_locations_found"},
            )

        # 2. Query latest S3InventoryObject records from the database
        with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
            stmt = select(S3InventoryObject)
            inventory_objects = session.exec(stmt).all()

        if not inventory_objects:
            return AnalysisResult(
                analyzer_name=self.name,
                provider=self.context.config.provider,
                recommendations=[],
                summary={"total_objects": 0},
            )

        # 3. Match each S3 object against known table location prefixes
        #    and collect Databricks-managed prefixes for orphan detection
        managed_prefixes = set(table_locations.keys())
        total = len(inventory_objects)
        matched_count = 0
        orphan_count = 0
        orphan_bytes = 0
        untagged_count = 0
        updates: list[tuple[S3InventoryObject, str | None, str | None, bool]] = []

        for obj in inventory_objects:
            obj_uri = f"s3://{obj.bucket}/{obj.key}"
            matched_table = None
            matched_location = None
            is_orphan = False

            # Check if object falls under a known table location prefix
            for loc_prefix, table_name in table_locations.items():
                if obj_uri.startswith(loc_prefix) or obj_uri == loc_prefix.rstrip("/"):
                    matched_table = table_name
                    matched_location = loc_prefix.rstrip("/")
                    break

            # If not matched, check if it's under any Databricks-managed path
            if matched_table is None:
                for prefix in managed_prefixes:
                    bucket_from_prefix = prefix.split("/")[2]
                    if obj.bucket == bucket_from_prefix:
                        is_orphan = True
                        orphan_count += 1
                        orphan_bytes += obj.size_bytes
                        break

            if matched_table:
                matched_count += 1

            if not obj.tags:
                untagged_count += 1

            updates.append((obj, matched_table, matched_location, is_orphan))

        # 4. Persist match results back to Postgres
        with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
            for obj, m_table, m_loc, orphan in updates:
                obj.matched_table = m_table
                obj.matched_table_location = m_loc
                obj.is_orphan = orphan
                session.add(obj)
            session.commit()

        # 5. Generate recommendations
        orphan_gb = orphan_bytes / (1024**3)
        orphan_monthly_cost = Decimal(str(orphan_gb)) * _S3_COST_PER_GB_MONTH

        if orphan_count > 0:
            recommendations.append(
                Recommendation(
                    type="orphan_s3_objects",
                    risk_level=RiskLevel.MEDIUM,
                    resource_id="s3-orphans",
                    resource_name="Orphaned S3 Objects",
                    title=f"{orphan_count} S3 objects not mapped to any Delta table",
                    description=(
                        f"Found {orphan_count} objects ({orphan_gb:.1f} GB) under "
                        f"Databricks-managed buckets that do not belong to any "
                        f"known Delta table. These may be leftover data from "
                        f"dropped tables or failed jobs."
                    ),
                    current_state={
                        "orphan_objects": orphan_count,
                        "orphan_gb": round(orphan_gb, 2),
                    },
                    recommended_state={"action": "review_and_delete_orphans"},
                    estimated_monthly_savings_usd=orphan_monthly_cost,
                    savings_confidence=SavingsConfidence.MEDIUM,
                )
            )

        if untagged_count > 0:
            recommendations.append(
                Recommendation(
                    type="untagged_s3_objects",
                    risk_level=RiskLevel.LOW,
                    resource_id="s3-untagged",
                    resource_name="Untagged S3 Objects",
                    title=f"{untagged_count} S3 objects missing tags",
                    description=(
                        f"{untagged_count} out of {total} objects under "
                        f"Databricks-managed paths have no S3 object tags. "
                        f"Tagging improves cost attribution and lifecycle management."
                    ),
                    current_state={"untagged_objects": untagged_count},
                    recommended_state={"action": "apply_tags"},
                    estimated_monthly_savings_usd=Decimal("0"),
                    savings_confidence=SavingsConfidence.LOW,
                )
            )

        return AnalysisResult(
            analyzer_name=self.name,
            provider=self.context.config.provider,
            recommendations=recommendations,
            summary={
                "total_objects": total,
                "matched_objects": matched_count,
                "matched_pct": round(matched_count / total * 100, 1) if total else 0,
                "orphan_objects": orphan_count,
                "orphan_gb": round(orphan_gb, 2),
                "untagged_objects": untagged_count,
            },
        )
