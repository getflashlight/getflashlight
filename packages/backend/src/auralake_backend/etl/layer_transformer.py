"""Layer transformation: raw → cleaned → enriched → aggregated.

Each transformation uses delete-and-replace + INSERT...SELECT with JOINs
to keep transformations in SQL for performance.
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa
import structlog
from sqlmodel import Session, delete, func, select

from auralake_backend.db.models import (
    AggBillingResourceMonthly,
    AggRecommendationSummary,
    BillingRecord,
    CleanedBillingSkuDay,
    ComputeResourceRecord,
    EnrichedBillingResource,
    EnrichedJobRun,
    EnrichedQuery,
    JobProfileRecord,
    JobRunRecord,
    QueryHistoryRecord,
    QueryPlan,
    RecommendationRecord,
)

logger = structlog.get_logger(__name__)


def _month_trunc(d: date) -> date:
    return d.replace(day=1)


class LayerTransformer:
    """Build cleaned/enriched/aggregated tables from raw inventory data."""

    def __init__(self, connection_id: uuid.UUID, session: Session) -> None:
        self.connection_id = connection_id
        self.session = session

    def run(self) -> dict[str, int]:
        results: dict[str, int] = {}
        results["cleaned_billing"] = self._build_cleaned_billing_sku_day()
        results["enriched_billing"] = self._build_enriched_billing_resource()
        results["enriched_job_runs"] = self._build_enriched_job_runs()
        results["enriched_queries"] = self._build_enriched_queries()
        results["agg_billing_monthly"] = self._build_agg_billing_monthly()
        results["agg_rec_summary"] = self._build_agg_recommendation_summary()
        self.session.flush()
        logger.info(
            "layer_transformation_completed",
            connection_id=str(self.connection_id),
            results=results,
        )
        return results

    # ------------------------------------------------------------------
    # Layer 2: cleaned.billing_sku_day
    # ------------------------------------------------------------------

    def _build_cleaned_billing_sku_day(self) -> int:
        # Delete existing rows for this connection
        self.session.execute(
            delete(CleanedBillingSkuDay).where(
                CleanedBillingSkuDay.connection_id == self.connection_id
            )
        )

        # Resolve resource_type and resource_id with SQL CASE
        resource_type_col = func.coalesce(
            # Priority order: job > cluster > warehouse > pipeline > endpoint
            func.nullif(
                func.case(
                    (BillingRecord.job_id.isnot(None), "job"),  # type: ignore[union-attr]
                    (
                        BillingRecord.cluster_id.isnot(None)  # type: ignore[union-attr]
                        & BillingRecord.job_id.is_(None),  # type: ignore[union-attr]
                        "cluster",
                    ),
                    (BillingRecord.warehouse_id.isnot(None), "warehouse"),  # type: ignore[union-attr]
                    (BillingRecord.pipeline_id.isnot(None), "pipeline"),  # type: ignore[union-attr]
                    (BillingRecord.endpoint_id.isnot(None), "endpoint"),  # type: ignore[union-attr]
                    else_="unknown",
                ),
                "",
            ),
            "unknown",
        )

        resource_id_col = func.coalesce(
            func.case(
                (BillingRecord.job_id.isnot(None), BillingRecord.job_id),  # type: ignore[union-attr]
                (
                    BillingRecord.cluster_id.isnot(None)  # type: ignore[union-attr]
                    & BillingRecord.job_id.is_(None),  # type: ignore[union-attr]
                    BillingRecord.cluster_id,
                ),
                (BillingRecord.warehouse_id.isnot(None), BillingRecord.warehouse_id),  # type: ignore[union-attr]
                (BillingRecord.pipeline_id.isnot(None), BillingRecord.pipeline_id),  # type: ignore[union-attr]
                (BillingRecord.endpoint_id.isnot(None), BillingRecord.endpoint_id),  # type: ignore[union-attr]
                else_=None,
            ),
            "unattributed",
        )

        rows = self.session.exec(
            select(
                BillingRecord.usage_date,
                BillingRecord.sku,
                resource_type_col.label("resource_type"),
                resource_id_col.label("resource_id"),
                func.max(BillingRecord.job_name).label("resource_name"),
                func.max(BillingRecord.workspace_id).label("workspace_id"),
                func.sum(BillingRecord.dbu_usage).label("dbu_usage"),
                func.sum(BillingRecord.cost_usd).label("cost_usd"),
                func.count().label("record_count"),
            )
            .where(BillingRecord.connection_id == self.connection_id)
            .group_by(
                BillingRecord.usage_date,
                BillingRecord.sku,
                resource_type_col,
                resource_id_col,
            )
        ).all()

        count = 0
        for row in rows:
            self.session.add(
                CleanedBillingSkuDay(
                    connection_id=self.connection_id,
                    usage_date=row[0],
                    sku=row[1],
                    resource_type=row[2],
                    resource_id=row[3],
                    resource_name=row[4],
                    workspace_id=row[5],
                    dbu_usage=float(row[6] or 0),
                    cost_usd=float(row[7] or 0),
                    record_count=int(row[8] or 0),
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Layer 3: enriched.billing_resource
    # ------------------------------------------------------------------

    def _build_enriched_billing_resource(self) -> int:
        self.session.execute(
            delete(EnrichedBillingResource).where(
                EnrichedBillingResource.connection_id == self.connection_id
            )
        )

        # First, get all cleaned billing rows
        cleaned_rows = self.session.exec(
            select(CleanedBillingSkuDay).where(
                CleanedBillingSkuDay.connection_id == self.connection_id
            )
        ).all()

        # Build compute resource lookup
        compute_rows = self.session.exec(
            select(ComputeResourceRecord).where(
                ComputeResourceRecord.connection_id == self.connection_id
            )
        ).all()

        compute_lookup: dict[str, ComputeResourceRecord] = {}
        for cr in compute_rows:
            compute_lookup[cr.resource_id] = cr

        count = 0
        for c in cleaned_rows:
            cr = compute_lookup.get(c.resource_id)
            self.session.add(
                EnrichedBillingResource(
                    connection_id=self.connection_id,
                    usage_date=c.usage_date,
                    sku=c.sku,
                    resource_type=c.resource_type,
                    resource_id=c.resource_id,
                    resource_name=c.resource_name or (cr.resource_name if cr else None),
                    workspace_id=c.workspace_id,
                    dbu_usage=c.dbu_usage,
                    cost_usd=c.cost_usd,
                    record_count=c.record_count,
                    creator=cr.creator if cr else None,
                    worker_node_type=cr.worker_node_type if cr else None,
                    num_workers=cr.num_workers if cr else None,
                    autoscale=cr.autoscale if cr else None,
                    spot_enabled=cr.spot_enabled if cr else None,
                    autotermination_minutes=cr.autotermination_minutes if cr else None,
                    warehouse_type=cr.warehouse_type if cr else None,
                    warehouse_size=cr.warehouse_size if cr else None,
                    compute_state=cr.state if cr else None,
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Layer 3: enriched.job_runs
    # ------------------------------------------------------------------

    def _build_enriched_job_runs(self) -> int:
        self.session.execute(
            delete(EnrichedJobRun).where(
                EnrichedJobRun.connection_id == self.connection_id
            )
        )

        # Get raw job runs
        job_runs = self.session.exec(
            select(JobRunRecord).where(JobRunRecord.connection_id == self.connection_id)
        ).all()

        # Build lookups
        job_profiles = self.session.exec(
            select(JobProfileRecord).where(
                JobProfileRecord.connection_id == self.connection_id
            )
        ).all()
        profile_lookup = {jp.job_id: jp for jp in job_profiles}

        compute_rows = self.session.exec(
            select(ComputeResourceRecord).where(
                ComputeResourceRecord.connection_id == self.connection_id
            )
        ).all()
        compute_lookup = {cr.resource_id: cr for cr in compute_rows}

        count = 0
        for jr in job_runs:
            jp = profile_lookup.get(jr.job_id)
            cr = compute_lookup.get(jr.cluster_id) if jr.cluster_id else None

            # Estimate run cost: prorate avg_dbu_cost by duration
            estimated_cost = None
            if jp and jp.avg_dbu_cost and jr.duration_ms:
                # avg_dbu_cost is per-run avg, duration_ms gives ratio
                estimated_cost = jp.avg_dbu_cost

            self.session.add(
                EnrichedJobRun(
                    connection_id=self.connection_id,
                    workspace_id=jr.workspace_id,
                    job_id=jr.job_id,
                    run_id=jr.run_id,
                    job_name=jp.job_name if jp else None,
                    state=jr.state,
                    start_time=jr.start_time,
                    end_time=jr.end_time,
                    duration_ms=jr.duration_ms,
                    cluster_id=jr.cluster_id,
                    trigger=jr.trigger,
                    error_message=jr.error_message,
                    schedule_cron=jp.schedule_cron if jp else None,
                    instance_type=jp.instance_type if jp else None,
                    worker_count=jp.worker_count if jp else None,
                    avg_dbu_cost=jp.avg_dbu_cost if jp else None,
                    is_portable=jp.is_portable if jp else None,
                    cluster_name=cr.resource_name if cr else None,
                    spot_enabled=cr.spot_enabled if cr else None,
                    cluster_creator=cr.creator if cr else None,
                    estimated_run_cost_usd=estimated_cost,
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Layer 3: enriched.queries
    # ------------------------------------------------------------------

    def _build_enriched_queries(self) -> int:
        self.session.execute(
            delete(EnrichedQuery).where(
                EnrichedQuery.connection_id == self.connection_id
            )
        )

        # Get query history
        queries = self.session.exec(
            select(QueryHistoryRecord).where(
                QueryHistoryRecord.connection_id == self.connection_id
            )
        ).all()

        # Build lookups
        plans = self.session.exec(
            select(QueryPlan).where(QueryPlan.connection_id == self.connection_id)
        ).all()
        plan_lookup = {qp.query_id: qp for qp in plans}

        compute_rows = self.session.exec(
            select(ComputeResourceRecord).where(
                ComputeResourceRecord.connection_id == self.connection_id,
                ComputeResourceRecord.resource_type == "sql_warehouse",
            )
        ).all()
        wh_lookup = {cr.resource_id: cr for cr in compute_rows}

        # Job profile lookup for job_name
        job_profiles = self.session.exec(
            select(JobProfileRecord).where(
                JobProfileRecord.connection_id == self.connection_id
            )
        ).all()
        jp_lookup = {jp.job_id: jp for jp in job_profiles}

        count = 0
        for qh in queries:
            qp = plan_lookup.get(qh.query_id)
            wh = wh_lookup.get(qh.warehouse_id) if qh.warehouse_id else None

            # Get anti-patterns count
            anti_patterns = qp.anti_patterns if qp else {}
            anti_pattern_list = (
                anti_patterns.get("patterns", [])
                if isinstance(anti_patterns, dict)
                else []
            )
            ap_count = (
                len(anti_pattern_list) if isinstance(anti_pattern_list, list) else 0
            )

            # Resolve job info from plan
            job_id = qp.job_id if qp else None
            jp = jp_lookup.get(job_id) if job_id else None

            self.session.add(
                EnrichedQuery(
                    connection_id=self.connection_id,
                    workspace_id=qh.workspace_id,
                    query_id=qh.query_id,
                    query_text=qh.query_text,
                    status=qh.status,
                    user_name=qh.user_name,
                    warehouse_id=qh.warehouse_id,
                    duration_ms=qh.duration_ms,
                    rows_produced=qh.rows_produced,
                    query_start_time_ms=qh.query_start_time_ms,
                    query_end_time_ms=qh.query_end_time_ms,
                    has_plan=qp is not None,
                    anti_patterns=anti_patterns,
                    anti_pattern_count=ap_count,
                    rows_scanned=qp.rows_scanned if qp else None,
                    bytes_read=qp.bytes_read if qp else None,
                    shuffle_bytes=qp.shuffle_bytes if qp else None,
                    spill_bytes=qp.spill_bytes if qp else None,
                    warehouse_name=wh.resource_name if wh else None,
                    warehouse_type=wh.warehouse_type if wh else None,
                    warehouse_size=wh.warehouse_size if wh else None,
                    estimated_cost_usd=None,  # Could be computed from billing
                    job_id=job_id,
                    job_name=jp.job_name if jp else None,
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Layer 4: aggregated.billing_resource_monthly
    # ------------------------------------------------------------------

    def _build_agg_billing_monthly(self) -> int:
        self.session.execute(
            delete(AggBillingResourceMonthly).where(
                AggBillingResourceMonthly.connection_id == self.connection_id
            )
        )

        # Aggregate from enriched billing by month
        month_col = func.date_trunc("month", EnrichedBillingResource.usage_date)
        rows = self.session.exec(
            select(
                month_col.label("month"),
                EnrichedBillingResource.sku,
                EnrichedBillingResource.resource_type,
                EnrichedBillingResource.resource_id,
                func.max(EnrichedBillingResource.resource_name).label("resource_name"),
                func.max(EnrichedBillingResource.creator).label("creator"),
                func.max(EnrichedBillingResource.workspace_id).label("workspace_id"),
                func.sum(EnrichedBillingResource.dbu_usage).label("dbu_usage"),
                func.sum(EnrichedBillingResource.cost_usd).label("cost_usd"),
                func.avg(EnrichedBillingResource.dbu_usage).label("avg_daily_dbu"),
                func.max(EnrichedBillingResource.dbu_usage).label("peak_daily_dbu"),
                func.count(func.distinct(EnrichedBillingResource.usage_date)).label(
                    "active_days"
                ),
                func.max(
                    func.cast(EnrichedBillingResource.spot_enabled, sa.Integer)
                ).label("spot_any"),
                func.max(EnrichedBillingResource.worker_node_type).label(
                    "worker_node_type"
                ),
                func.max(EnrichedBillingResource.warehouse_type).label(
                    "warehouse_type"
                ),
            )
            .where(EnrichedBillingResource.connection_id == self.connection_id)
            .group_by(
                month_col,
                EnrichedBillingResource.sku,
                EnrichedBillingResource.resource_type,
                EnrichedBillingResource.resource_id,
            )
        ).all()

        # Build lookup for prev month cost
        month_cost: dict[tuple[str, str, str, str], float] = {}
        for row in rows:
            month_val = row[0]
            if hasattr(month_val, "date"):
                month_val = month_val.date()
            month_val = _month_trunc(month_val)
            key = (month_val.isoformat(), row[1], row[2], row[3])
            month_cost[key] = float(row[8] or 0)

        count = 0
        for row in rows:
            month_val = row[0]
            if hasattr(month_val, "date"):
                month_val = month_val.date()
            month_val = _month_trunc(month_val)

            # Find prev month
            if month_val.month == 1:
                prev_month = date(month_val.year - 1, 12, 1)
            else:
                prev_month = date(month_val.year, month_val.month - 1, 1)

            prev_key = (prev_month.isoformat(), row[1], row[2], row[3])
            prev_cost = month_cost.get(prev_key)
            curr_cost = float(row[8] or 0)

            cost_change_pct = None
            if prev_cost and prev_cost > 0:
                cost_change_pct = ((curr_cost - prev_cost) / prev_cost) * 100

            spot_val = row[12]
            spot_enabled = bool(spot_val) if spot_val is not None else None

            self.session.add(
                AggBillingResourceMonthly(
                    connection_id=self.connection_id,
                    month=month_val,
                    sku=row[1],
                    resource_type=row[2],
                    resource_key=row[3],
                    resource_name=row[4],
                    creator=row[5],
                    workspace_id=row[6],
                    dbu_usage=float(row[7] or 0),
                    cost_usd=curr_cost,
                    resource_ids=[row[3]],
                    avg_daily_dbu=float(row[9]) if row[9] else None,
                    peak_daily_dbu=float(row[10]) if row[10] else None,
                    active_days=int(row[11]) if row[11] else None,
                    spot_enabled=spot_enabled,
                    worker_node_type=row[13],
                    warehouse_type=row[14],
                    prev_month_cost_usd=prev_cost,
                    cost_change_pct=round(cost_change_pct, 2)
                    if cost_change_pct is not None
                    else None,
                )
            )
            count += 1
        return count

    # ------------------------------------------------------------------
    # Layer 4: aggregated.recommendation_summary
    # ------------------------------------------------------------------

    def _build_agg_recommendation_summary(self) -> int:
        self.session.execute(
            delete(AggRecommendationSummary).where(
                AggRecommendationSummary.connection_id == self.connection_id
            )
        )

        # Aggregate recommendations by type and month
        month_col = func.date_trunc("month", RecommendationRecord.created_at)
        rows = self.session.exec(
            select(
                month_col.label("month"),
                RecommendationRecord.type,
                func.count().label("count"),
                func.sum(RecommendationRecord.estimated_monthly_savings_usd).label(
                    "total_estimated"
                ),
                func.sum(
                    func.coalesce(RecommendationRecord.actual_monthly_savings_usd, 0.0)
                ).label("total_actual"),
                func.sum(
                    func.case(
                        (RecommendationRecord.status == "applied", 1),
                        else_=0,
                    )
                ).label("applied"),
                func.sum(
                    func.case(
                        (RecommendationRecord.status == "dismissed", 1),
                        else_=0,
                    )
                ).label("dismissed"),
            ).group_by(month_col, RecommendationRecord.type)
        ).all()

        count = 0
        for row in rows:
            month_val = row[0]
            if hasattr(month_val, "date"):
                month_val = month_val.date()
            month_val = _month_trunc(month_val)

            self.session.add(
                AggRecommendationSummary(
                    connection_id=self.connection_id,
                    month=month_val,
                    recommendation_type=row[1],
                    count=int(row[2] or 0),
                    total_estimated_savings_usd=float(row[3] or 0),
                    total_actual_savings_usd=float(row[4] or 0),
                    applied_count=int(row[5] or 0),
                    dismissed_count=int(row[6] or 0),
                )
            )
            count += 1
        return count
