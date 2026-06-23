"""Savings verification using already-collected billing and infra data.

Compares cost for a resource in the 30 days before a recommendation was
applied versus the most recent 30 days of collected data.  No separate
collection job is needed — this reads from billing_records and
infra_cost_snapshots that FullCollector already populates.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import structlog
from sqlmodel import Session, func, select

from auralake_backend.db.models import (
    BillingRecord,
    InfraCostSnapshot,
    RecommendationRecord,
)

logger = structlog.get_logger(__name__)

# Minimum days after application before we attempt verification
_MIN_POST_DAYS = 14
# Window size (days) for before/after comparison
_WINDOW_DAYS = 30


class SavingsTracker:
    """Compute actual savings for applied recommendations from collected data."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def verify_applied_recommendations(self) -> int:
        """Re-compute actual savings for all applied recommendations.

        Skips recommendations applied less than ``_MIN_POST_DAYS`` ago
        (not enough post-data yet).  Returns the number of records updated.
        """
        cutoff = datetime.now(UTC) - timedelta(days=_MIN_POST_DAYS)
        recs = self.session.exec(
            select(RecommendationRecord)
            .where(RecommendationRecord.status == "applied")
            .where(RecommendationRecord.applied_at.isnot(None))  # type: ignore[union-attr]
            .where(RecommendationRecord.applied_at < cutoff)  # type: ignore[operator]
        ).all()

        updated = 0
        for rec in recs:
            try:
                result = self._compute_savings(rec)
                if result is not None:
                    baseline, actual_savings = result
                    rec.baseline_monthly_cost_usd = baseline
                    rec.actual_monthly_savings_usd = actual_savings
                    rec.savings_verified_at = datetime.now(UTC)
                    self.session.add(rec)
                    updated += 1
            except Exception as exc:
                logger.warning(
                    "savings_verification_failed",
                    recommendation_id=str(rec.id),
                    error=str(exc),
                )

        if updated:
            self.session.commit()
            logger.info("savings_verified", updated=updated)

        return updated

    def _compute_savings(self, rec: RecommendationRecord) -> tuple[float, float] | None:
        """Return (baseline_monthly_cost, actual_monthly_savings) or None.

        Looks up the resource_id in billing_records (for DBU-based costs)
        and infra_cost_snapshots (for AWS infra costs), comparing the
        window before ``applied_at`` to the most recent window.
        """
        if rec.applied_at is None:
            return None

        applied = (
            rec.applied_at.date()
            if isinstance(rec.applied_at, datetime)
            else rec.applied_at
        )

        pre_start = applied - timedelta(days=_WINDOW_DAYS)
        pre_end = applied
        post_start = applied
        post_end = date.today()

        # Not enough post-application data yet
        if (post_end - post_start).days < _MIN_POST_DAYS:
            return None

        resource_id = rec.resource_id
        rec_type = rec.type

        # Route to the right cost table based on recommendation type
        if rec_type.startswith("infra_") or rec_type.startswith("orphan_s3"):
            return self._compare_infra_costs(
                resource_id, pre_start, pre_end, post_start, post_end
            )

        return self._compare_billing_costs(
            resource_id, rec_type, pre_start, pre_end, post_start, post_end
        )

    def _compare_billing_costs(
        self,
        resource_id: str,
        rec_type: str,
        pre_start: date,
        pre_end: date,
        post_start: date,
        post_end: date,
    ) -> tuple[float, float] | None:
        """Compare DBU billing costs before/after for a resource."""
        # Determine which billing column to match on
        col = self._billing_column_for_type(rec_type)
        if col is None:
            return None

        pre_cost = self._sum_billing(col, resource_id, pre_start, pre_end)
        post_cost = self._sum_billing(col, resource_id, post_start, post_end)

        if pre_cost is None:
            return None

        # Normalize to monthly rate
        pre_days = max((pre_end - pre_start).days, 1)
        post_days = max((post_end - post_start).days, 1)

        baseline_monthly = (pre_cost / pre_days) * 30
        post_monthly = ((post_cost or 0.0) / post_days) * 30
        actual_savings = baseline_monthly - post_monthly

        return (round(baseline_monthly, 2), round(actual_savings, 2))

    def _sum_billing(
        self, column_name: str, resource_id: str, start: date, end: date
    ) -> float | None:
        """Sum cost_usd from billing_records for a resource in a date range."""
        col = getattr(BillingRecord, column_name, None)
        if col is None:
            return None

        result = self.session.exec(
            select(func.sum(BillingRecord.cost_usd))
            .where(col == resource_id)
            .where(BillingRecord.usage_date >= start)
            .where(BillingRecord.usage_date < end)
        ).one_or_none()

        return float(result) if result is not None else None

    def _compare_infra_costs(
        self,
        resource_id: str,
        pre_start: date,
        pre_end: date,
        post_start: date,
        post_end: date,
    ) -> tuple[float, float] | None:
        """Compare AWS infra costs before/after for a resource."""
        pre_cost = self._sum_infra(resource_id, pre_start, pre_end)
        post_cost = self._sum_infra(resource_id, post_start, post_end)

        if pre_cost is None:
            return None

        pre_days = max((pre_end - pre_start).days, 1)
        post_days = max((post_end - post_start).days, 1)

        baseline_monthly = (pre_cost / pre_days) * 30
        post_monthly = ((post_cost or 0.0) / post_days) * 30
        actual_savings = baseline_monthly - post_monthly

        return (round(baseline_monthly, 2), round(actual_savings, 2))

    def _sum_infra(self, resource_id: str, start: date, end: date) -> float | None:
        """Sum cost_usd from infra_cost_snapshots for a resource."""
        result = self.session.exec(
            select(func.sum(InfraCostSnapshot.cost_usd))
            .where(InfraCostSnapshot.resource_id == resource_id)
            .where(InfraCostSnapshot.period_start >= start)
            .where(InfraCostSnapshot.period_start < end)
        ).one_or_none()

        return float(result) if result is not None else None

    @staticmethod
    def _billing_column_for_type(rec_type: str) -> str | None:
        """Map recommendation type to the billing_records column to filter on."""
        mapping: dict[str, str] = {
            "cluster_rightsize": "cluster_id",
            "cluster_idle": "cluster_id",
            "cluster_no_autotermination": "cluster_id",
            "cluster_spot_eligible": "cluster_id",
            "spot_eligible": "cluster_id",
            "idle_cluster": "cluster_id",
            "cost_high_sku": "sku",
            "job_stale": "job_id",
            "job_failing": "job_id",
            "job_consolidation": "job_id",
            "query_expensive": "warehouse_id",
        }
        return mapping.get(rec_type)
