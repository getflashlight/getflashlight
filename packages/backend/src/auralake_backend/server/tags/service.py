from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from sqlmodel import Session, func, select

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import S3InventoryObject

from .schemas import (
    S3InventoryCollectResponse,
    S3InventoryObjectResponse,
    S3InventoryStatusResponse,
)


class TagService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def scan(self) -> AnalysisResult:
        from auralake_backend.analyzers.tag_analyzer import TagAnalyzer

        return TagAnalyzer(self.context).analyze()

    def report(self) -> AnalysisResult:
        from auralake_backend.analyzers.tag_analyzer import TagAnalyzer

        return TagAnalyzer(self.context).analyze()

    def enforce(self) -> list[ActionResult]:
        from auralake_backend.actions.tag_actions import EnforceTagsAction
        from auralake_backend.analyzers.tag_analyzer import TagAnalyzer

        result = TagAnalyzer(self.context).analyze()
        action = EnforceTagsAction(self.context)
        action_results: list[ActionResult] = []
        for rec in result.recommendations:
            action_results.append(action.execute(rec))
        return action_results

    # ------------------------------------------------------------------
    # S3 Inventory
    # ------------------------------------------------------------------

    def s3_inventory_status(self) -> S3InventoryStatusResponse:
        """Return summary of S3 inventory collection state."""
        with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
            total = session.exec(select(func.count()).select_from(S3InventoryObject)).one()
            matched = session.exec(
                select(func.count())
                .select_from(S3InventoryObject)
                .where(S3InventoryObject.matched_table != None)  # noqa: E711
            ).one()
            orphans = session.exec(
                select(func.count())
                .select_from(S3InventoryObject)
                .where(S3InventoryObject.is_orphan == True)  # noqa: E712
            ).one()
            latest_date_row = session.exec(select(func.max(S3InventoryObject.collected_at))).one()

            # Get distinct buckets
            bucket_rows = session.exec(select(S3InventoryObject.bucket).distinct()).all()

        return S3InventoryStatusResponse(
            configured_buckets=list(bucket_rows),
            latest_report_date=latest_date_row,
            total_objects=total,
            matched_objects=matched,
            orphan_objects=orphans,
        )

    def s3_inventory_collect(self) -> S3InventoryCollectResponse:
        """Trigger on-demand S3 inventory collection."""
        from auralake_backend.etl.inventory_collector import InventoryCollector

        with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
            collector = InventoryCollector(self.context, session)
            count = collector._collect_s3_inventory()  # noqa: SLF001
            buckets = collector._discover_databricks_buckets()  # noqa: SLF001

        # Count matched/orphan from DB after collection
        with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
            matched = session.exec(
                select(func.count())
                .select_from(S3InventoryObject)
                .where(S3InventoryObject.matched_table != None)  # noqa: E711
            ).one()
            orphans = session.exec(
                select(func.count())
                .select_from(S3InventoryObject)
                .where(S3InventoryObject.is_orphan == True)  # noqa: E712
            ).one()

        return S3InventoryCollectResponse(
            objects_ingested=count,
            objects_matched=matched,
            objects_orphaned=orphans,
            buckets_processed=buckets,
        )

    def s3_tag_report(self, *, orphans_only: bool = False) -> list[S3InventoryObjectResponse]:
        """List S3 objects with their table mapping status."""
        with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
            stmt = select(S3InventoryObject)
            if orphans_only:
                stmt = stmt.where(
                    S3InventoryObject.is_orphan == True  # noqa: E712
                )
            rows = session.exec(stmt).all()

        return [
            S3InventoryObjectResponse(
                bucket=row.bucket,
                key=row.key,
                size_bytes=row.size_bytes,
                matched_table=row.matched_table,
                is_orphan=row.is_orphan,
                tags=row.tags,
                collected_at=row.collected_at,
            )
            for row in rows
        ]
