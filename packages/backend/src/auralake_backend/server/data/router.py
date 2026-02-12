"""Read-only data view endpoints for collected data."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Generator
from datetime import date

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlmodel import Session, func, select

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import (
    ApiKey,
    BillingRecord,
    BillingResourceMonthly,
    ClusterPolicyRecord,
    InfraResourceMapping,
    JobProfileRecord,
    JobRunRecord,
    QueryHistoryRecord,
    QueryPlan,
    RecommendationRecord,
    S3InventoryObject,
)
from auralake_backend.server.auth import require_auth

logger = structlog.get_logger(__name__)

router = APIRouter()


def _get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


@router.get("/summary")
async def data_summary(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> dict:
    """Counts of all collected data."""
    clusters = session.exec(
        select(func.count()).where(InfraResourceMapping.platform_resource_type == "cluster")
    ).one()
    jobs = session.exec(select(func.count()).select_from(JobProfileRecord)).one()
    job_runs = session.exec(select(func.count()).select_from(JobRunRecord)).one()
    billing = session.exec(select(func.count()).select_from(BillingRecord)).one()
    queries = session.exec(select(func.count()).select_from(QueryHistoryRecord)).one()
    recommendations = session.exec(select(func.count()).select_from(RecommendationRecord)).one()

    query_plans = session.exec(select(func.count()).select_from(QueryPlan)).one()
    policies = session.exec(select(func.count()).select_from(ClusterPolicyRecord)).one()
    s3_objects = session.exec(select(func.count()).select_from(S3InventoryObject)).one()

    return {
        "clusters": clusters,
        "jobs": jobs,
        "job_runs": job_runs,
        "billing_records": billing,
        "queries": queries,
        "query_plans": query_plans,
        "policies": policies,
        "s3_inventory_objects": s3_objects,
        "recommendations": recommendations,
    }


@router.get("/clusters")
async def list_clusters(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Collected cluster data."""
    rows = session.exec(
        select(InfraResourceMapping)
        .where(InfraResourceMapping.platform_resource_type == "cluster")
        .order_by(InfraResourceMapping.last_seen_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "cluster_id": r.platform_resource_id,
            "workspace_id": r.workspace_id,
            "provider": r.provider,
            "tags": r.infra_resource_tags,
            "hourly_cost_usd": r.hourly_cost_usd,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in rows
    ]


@router.get("/jobs")
async def list_jobs(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Collected job profiles."""
    rows = session.exec(
        select(JobProfileRecord)
        .order_by(JobProfileRecord.last_analyzed_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "job_id": r.job_id,
            "job_name": r.job_name,
            "workspace_id": r.workspace_id,
            "schedule_cron": r.schedule_cron,
            "avg_duration_minutes": r.avg_duration_minutes,
            "avg_dbu_cost": r.avg_dbu_cost,
            "instance_type": r.instance_type,
            "worker_count": r.worker_count,
            "is_portable": r.is_portable,
            "last_analyzed_at": (r.last_analyzed_at.isoformat() if r.last_analyzed_at else None),
        }
        for r in rows
    ]


@router.get("/jobs/{job_id}/runs")
async def list_job_runs(
    job_id: str,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Job run history for a specific job."""
    rows = session.exec(
        select(JobRunRecord)
        .where(JobRunRecord.job_id == job_id)
        .order_by(JobRunRecord.start_time.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "run_id": r.run_id,
            "job_id": r.job_id,
            "state": r.state,
            "start_time": r.start_time.isoformat() if r.start_time else None,
            "end_time": r.end_time.isoformat() if r.end_time else None,
            "duration_ms": r.duration_ms,
            "cluster_id": r.cluster_id,
            "trigger": r.trigger,
            "error_message": r.error_message,
        }
        for r in rows
    ]


def _months_back(ref: date, n: int) -> date:
    """First day of the month *n* months before *ref*'s month."""
    m = ref.month - n
    y = ref.year
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


_MIN_CONTRIBUTOR_DBU_CHANGE = 500


def _compute_reasons(
    session: Session,
    contributors: list[dict],
    prev_date: date,
    curr_date: date,
) -> None:
    """Enrich contributors with a human-readable 'reason' field in-place."""

    def _next_month_first(d: date) -> date:
        return date(d.year + (d.month // 12), (d.month % 12) + 1, 1)

    prev_start = prev_date.replace(day=1)
    prev_end = _next_month_first(prev_start)
    curr_start = curr_date.replace(day=1)
    curr_end = _next_month_first(curr_start)

    # Collect all underlying job_ids from resource_ids for job-type contributors
    all_job_ids: list[str] = []
    for c in contributors:
        if c["type"] == "job":
            all_job_ids.extend(c.get("resource_ids", []))

    prev_runs: dict[str, int] = {}
    curr_runs: dict[str, int] = {}
    if all_job_ids:
        for target_start, target_end, target_dict in [
            (prev_start, prev_end, prev_runs),
            (curr_start, curr_end, curr_runs),
        ]:
            rows = session.exec(
                select(JobRunRecord.job_id, func.count())
                .where(
                    JobRunRecord.job_id.in_(all_job_ids),  # type: ignore[union-attr]
                    JobRunRecord.start_time >= target_start,  # type: ignore[operator]
                    JobRunRecord.start_time < target_end,  # type: ignore[operator]
                )
                .group_by(JobRunRecord.job_id)
            ).all()
            for r in rows:
                target_dict[r[0]] = r[1]

    # Batch fetch applied recommendations for resource_keys and resource_ids
    all_keys = [c["resource_key"] for c in contributors]
    all_resource_ids: list[str] = []
    for c in contributors:
        all_resource_ids.extend(c.get("resource_ids", []))
    lookup_ids = list(set(all_keys + all_resource_ids))

    applied_recs: dict[str, str] = {}
    if lookup_ids:
        rows = session.exec(
            select(
                RecommendationRecord.resource_id,
                RecommendationRecord.title,
            ).where(
                RecommendationRecord.resource_id.in_(lookup_ids),  # type: ignore[union-attr]
                RecommendationRecord.status == "applied",
            )
        ).all()
        for r in rows:
            applied_recs[r[0]] = r[1]

    for c in contributors:
        reasons: list[str] = []

        # New / Removed
        if c["prev_dbu"] == 0 and c["dbu"] > 0:
            reasons.append("New")
        elif c["dbu"] == 0 and c["prev_dbu"] > 0:
            reasons.append("Removed")

        # Run count change (jobs only) — aggregate across all underlying job_ids
        if c["type"] == "job":
            rids = c.get("resource_ids", [])
            p = sum(prev_runs.get(jid, 0) for jid in rids)
            n = sum(curr_runs.get(jid, 0) for jid in rids)
            prev_has_data = p > 0 or c["prev_dbu"] == 0
            curr_has_data = n > 0 or c["dbu"] == 0
            if (p or n) and prev_has_data and curr_has_data:
                reasons.append(f"Runs: {p} \u2192 {n}")

        # Applied optimization — check resource_key and all resource_ids
        matched_rec = applied_recs.get(c["resource_key"])
        if not matched_rec:
            for rid in c.get("resource_ids", []):
                matched_rec = applied_recs.get(rid)
                if matched_rec:
                    break
        if matched_rec:
            reasons.append(f"Applied: {matched_rec}")

        c["reason"] = "; ".join(reasons) if reasons else None


def _compute_insights(
    session: Session,
    monthly_by_sku: list[dict],
    by_sku_rows: list,
) -> list[dict]:
    """Compare two most recent complete months and surface biggest movers."""
    # Find the two most recent months
    months = sorted({r["month"] for r in monthly_by_sku})
    if len(months) < 2:
        return []

    curr_month = months[-1]
    prev_month = months[0]

    # Build month+sku -> dbu lookup
    sku_month_dbu: dict[str, dict[str, float]] = defaultdict(dict)
    for r in monthly_by_sku:
        sku_month_dbu[r["sku"]][r["month"]] = r["dbu_usage"]

    # Top 10 SKUs by total DBU
    top_skus = [row[0] for row in by_sku_rows[:10]]

    insights = []
    for sku in top_skus:
        curr_dbu = sku_month_dbu.get(sku, {}).get(curr_month, 0.0)
        prev_dbu = sku_month_dbu.get(sku, {}).get(prev_month, 0.0)
        change_dbu = curr_dbu - prev_dbu
        change_pct = (change_dbu / prev_dbu * 100) if prev_dbu else 0.0

        # Top contributors: break down by job_id and cluster_id for this SKU
        month_col = func.date_trunc("month", BillingRecord.usage_date)
        top_contributors = _get_top_contributors(session, sku, month_col, curr_month, prev_month)

        insights.append(
            {
                "sku": sku,
                "prev_month": prev_month,
                "curr_month": curr_month,
                "prev_dbu": prev_dbu,
                "curr_dbu": curr_dbu,
                "change_dbu": round(change_dbu, 2),
                "change_pct": round(change_pct, 2),
                "top_contributors": top_contributors,
            }
        )

    return insights


def _get_top_contributors(
    session: Session,
    sku: str,
    month_col,  # noqa: ANN001
    curr_month: str,
    prev_month: str,
) -> list[dict]:
    """Find top resources driving MoM change for a given SKU.

    Queries the pre-aggregated billing_resource_monthly table instead of
    computing on-the-fly from raw billing_records.
    """
    curr_date = date.fromisoformat(f"{curr_month}-01")
    prev_date = date.fromisoformat(f"{prev_month}-01")

    rows = session.exec(
        select(
            BillingResourceMonthly.resource_type,
            BillingResourceMonthly.resource_key,
            BillingResourceMonthly.resource_name,
            BillingResourceMonthly.month,
            BillingResourceMonthly.dbu_usage,
            BillingResourceMonthly.resource_ids,
        ).where(
            BillingResourceMonthly.sku == sku,
            BillingResourceMonthly.month.in_([prev_date, curr_date]),  # type: ignore[union-attr]
        )
    ).all()

    # Pivot by resource_key: collect prev/curr month DBU
    resource_data: dict[str, dict] = {}
    for row in rows:
        rkey = row[1]
        if rkey not in resource_data:
            resource_data[rkey] = {
                "type": row[0],
                "resource_key": rkey,
                "name": row[2] or rkey,
                "prev_dbu": 0.0,
                "curr_dbu": 0.0,
                "resource_ids": [],
            }
        if row[3] == prev_date:
            resource_data[rkey]["prev_dbu"] = float(row[4] or 0)
        elif row[3] == curr_date:
            resource_data[rkey]["curr_dbu"] = float(row[4] or 0)
        # Merge resource_ids from both months
        for rid in row[5] or []:
            if rid not in resource_data[rkey]["resource_ids"]:
                resource_data[rkey]["resource_ids"].append(rid)

    # Compute changes and filter
    contributors: list[dict] = []
    for rkey, data in resource_data.items():
        curr_dbu = data["curr_dbu"]
        prev_dbu = data["prev_dbu"]
        change_dbu = curr_dbu - prev_dbu
        change_pct = ((change_dbu) / prev_dbu * 100) if prev_dbu else (100.0 if curr_dbu else 0.0)

        if abs(change_pct) < 20:
            continue
        if abs(change_dbu) < _MIN_CONTRIBUTOR_DBU_CHANGE:
            continue

        contributors.append(
            {
                "type": data["type"],
                "resource_key": rkey,
                "name": data["name"],
                "prev_dbu": round(prev_dbu, 2),
                "dbu": round(curr_dbu, 2),
                "change_dbu": round(change_dbu, 2),
                "change_pct": round(change_pct, 1),
                "resource_ids": data["resource_ids"],
            }
        )

    contributors.sort(key=lambda x: abs(x["change_dbu"]), reverse=True)
    result = contributors[:5]

    _compute_reasons(session, result, prev_date, curr_date)
    return result


@router.get("/billing")
async def billing_summary(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
) -> dict:
    """Billing summary — total cost, DBU usage, and breakdown by SKU."""
    # Default: last 3 months from the most recent data point
    if from_date is None or to_date is None:
        max_d = session.exec(select(func.max(BillingRecord.usage_date))).one()
        if max_d:
            if to_date is None:
                to_date = max_d
            if from_date is None:
                from_date = _months_back(max_d, 2)

    def _date_filter(stmt):  # noqa: ANN001, ANN202
        if from_date:
            stmt = stmt.where(BillingRecord.usage_date >= from_date)
        if to_date:
            stmt = stmt.where(BillingRecord.usage_date <= to_date)
        return stmt

    stmt_cost = select(func.sum(BillingRecord.cost_usd))
    stmt_dbu = select(func.sum(BillingRecord.dbu_usage))
    total = session.exec(_date_filter(stmt_cost)).one() or 0.0
    total_dbu = session.exec(_date_filter(stmt_dbu)).one() or 0.0

    stmt_sku = (
        select(
            BillingRecord.sku,
            func.sum(BillingRecord.cost_usd),
            func.sum(BillingRecord.dbu_usage),
        )
        .group_by(BillingRecord.sku)
        .order_by(func.sum(BillingRecord.dbu_usage).desc())
    )
    by_sku_rows = session.exec(_date_filter(stmt_sku)).all()

    # Monthly trend by SKU
    month_col = func.date_trunc("month", BillingRecord.usage_date)
    stmt_monthly = (
        select(
            month_col,
            BillingRecord.sku,
            func.sum(BillingRecord.cost_usd),
            func.sum(BillingRecord.dbu_usage),
        )
        .group_by(month_col, BillingRecord.sku)
        .order_by(month_col)
    )
    monthly_sku_rows = session.exec(_date_filter(stmt_monthly)).all()

    monthly_by_sku = [
        {
            "month": row[0].strftime("%Y-%m"),
            "sku": row[1],
            "cost_usd": float(row[2]),
            "dbu_usage": float(row[3]),
        }
        for row in monthly_sku_rows
    ]

    # Date range (reflects the filtered window)
    stmt_range = select(
        func.min(BillingRecord.usage_date),
        func.max(BillingRecord.usage_date),
    )
    date_range_row = session.exec(_date_filter(stmt_range)).one()
    date_range = None
    if date_range_row[0] and date_range_row[1]:
        date_range = {
            "min": date_range_row[0].strftime("%Y-%m"),
            "max": date_range_row[1].strftime("%Y-%m"),
        }

    # Insights: compare first and last month in the filtered range
    insights = _compute_insights(session, monthly_by_sku, by_sku_rows)

    return {
        "total_cost_usd": float(total),
        "total_dbu_usage": float(total_dbu),
        "by_sku": {row[0]: float(row[1]) for row in by_sku_rows},
        "by_sku_dbu": {row[0]: float(row[2]) for row in by_sku_rows},
        "monthly_by_sku": monthly_by_sku,
        "date_range": date_range,
        "insights": insights,
    }


@router.get("/billing/validate")
async def billing_validate(
    request: Request,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    from_date: date = Query(...),
    to_date: date = Query(...),
    sku: str | None = Query(default=None),
) -> dict:
    """Compare local billing records against Databricks system.billing.usage."""
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="Server not configured")

    # Query Databricks directly
    from auralake_backend.providers.databricks.auth import get_warehouse_id, get_workspace_client

    sku_filter = f"AND sku_name = '{sku}'" if sku else ""
    sql = (
        f"SELECT DATE_TRUNC('month', usage_date) AS month, sku_name, "
        f"SUM(usage_quantity) AS dbu_usage "
        f"FROM system.billing.usage "
        f"WHERE usage_date BETWEEN '{from_date}' AND '{to_date}' "
        f"{sku_filter} "
        f"GROUP BY 1, 2 ORDER BY 1, 2"
    )

    databricks_rows: list[dict] = []
    databricks_errors: list[str] = []
    for ws_name, ws_config in config.databricks.workspaces.items():
        try:
            client = get_workspace_client(config.databricks, ws_name)
            wh_id = get_warehouse_id(client, ws_config.sql_warehouse_id)
            result = client.statement_execution.execute_statement(
                warehouse_id=wh_id, statement=sql
            )
            if result.result and result.result.data_array:
                columns = [col.name for col in (result.manifest.schema.columns or [])]  # type: ignore[union-attr]
                databricks_rows = [dict(zip(columns, row)) for row in result.result.data_array]
            databricks_errors = []
            break
        except Exception as exc:
            databricks_errors.append(f"{ws_name}: {exc}")
            logger.warning("billing_validate_workspace_failed", workspace=ws_name, error=str(exc))
    if databricks_errors and not databricks_rows:
        logger.error("billing_validate_all_workspaces_failed", errors=databricks_errors)

    # Query local BillingRecord table with matching grouping
    month_col = func.date_trunc("month", BillingRecord.usage_date)
    local_stmt = (
        select(
            month_col.label("month"),
            BillingRecord.sku,
            func.sum(BillingRecord.dbu_usage),
        )
        .where(
            BillingRecord.usage_date >= from_date,
            BillingRecord.usage_date <= to_date,
        )
        .group_by(month_col, BillingRecord.sku)
        .order_by(month_col, BillingRecord.sku)
    )
    if sku:
        local_stmt = local_stmt.where(BillingRecord.sku == sku)
    local_rows_raw = session.exec(local_stmt).all()

    local_data = [
        {
            "month": row[0].strftime("%Y-%m"),
            "sku": row[1],
            "dbu_usage": float(row[2] or 0),
        }
        for row in local_rows_raw
    ]

    db_data = [
        {
            "month": r.get("month", "")[:7] if r.get("month") else "",
            "sku": r.get("sku_name", ""),
            "dbu_usage": float(r.get("dbu_usage", 0)),
        }
        for r in databricks_rows
    ]

    # Compute discrepancies
    db_lookup: dict[tuple[str, str], float] = {
        (d["month"], d["sku"]): d["dbu_usage"] for d in db_data
    }
    local_lookup: dict[tuple[str, str], float] = {
        (d["month"], d["sku"]): d["dbu_usage"] for d in local_data
    }
    all_keys = set(db_lookup.keys()) | set(local_lookup.keys())

    discrepancies = []
    for key in sorted(all_keys):
        db_val = db_lookup.get(key, 0.0)
        local_val = local_lookup.get(key, 0.0)
        diff = local_val - db_val
        if abs(diff) > 0.01:
            pct = (diff / db_val * 100) if db_val else None
            discrepancies.append(
                {
                    "month": key[0],
                    "sku": key[1],
                    "databricks_dbu": round(db_val, 2),
                    "local_dbu": round(local_val, 2),
                    "diff_dbu": round(diff, 2),
                    "diff_pct": round(pct, 2) if pct is not None else None,
                }
            )

    result: dict = {
        "databricks": db_data,
        "local": local_data,
        "discrepancies": discrepancies,
    }
    if databricks_errors and not db_data:
        result["errors"] = databricks_errors
    return result


@router.get("/recommendations")
async def list_recommendations(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Generated recommendations from analysis."""
    stmt = select(RecommendationRecord)
    if status:
        stmt = stmt.where(RecommendationRecord.status == status)
    stmt = (
        stmt.order_by(
            RecommendationRecord.estimated_monthly_savings_usd.desc()  # type: ignore[union-attr]
        )
        .offset(offset)
        .limit(limit)
    )

    rows = session.exec(stmt).all()
    return [
        {
            "id": str(r.id),
            "type": r.type,
            "risk_level": r.risk_level,
            "resource_id": r.resource_id,
            "resource_name": r.resource_name,
            "title": r.title,
            "description": r.description,
            "estimated_monthly_savings_usd": r.estimated_monthly_savings_usd,
            "savings_confidence": r.savings_confidence,
            "pricing_basis": r.pricing_basis,
            "baseline_monthly_cost_usd": r.baseline_monthly_cost_usd,
            "actual_monthly_savings_usd": r.actual_monthly_savings_usd,
            "savings_verified_at": r.savings_verified_at.isoformat()
            if r.savings_verified_at
            else None,
            "status": r.status,
            "applied_at": r.applied_at.isoformat() if r.applied_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.get("/queries")
async def list_queries(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Collected query history."""
    rows = session.exec(
        select(QueryHistoryRecord)
        .order_by(QueryHistoryRecord.created_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "query_id": r.query_id,
            "workspace_id": r.workspace_id,
            "query_text": r.query_text,
            "status": r.status,
            "user_name": r.user_name,
            "warehouse_id": r.warehouse_id,
            "duration_ms": r.duration_ms,
            "rows_produced": r.rows_produced,
            "query_start_time_ms": r.query_start_time_ms,
            "query_end_time_ms": r.query_end_time_ms,
        }
        for r in rows
    ]


@router.get("/query-plans")
async def list_query_plans(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Stored Spark query plans with anti-pattern detection."""
    rows = session.exec(
        select(QueryPlan)
        .order_by(QueryPlan.captured_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "id": str(r.id),
            "query_id": r.query_id,
            "workspace_id": r.workspace_id,
            "query_text": r.query_text,
            "anti_patterns": r.anti_patterns,
            "duration_ms": r.duration_ms,
            "rows_scanned": r.rows_scanned,
            "bytes_read": r.bytes_read,
            "shuffle_bytes": r.shuffle_bytes,
            "spill_bytes": r.spill_bytes,
            "captured_at": r.captured_at.isoformat() if r.captured_at else None,
        }
        for r in rows
    ]


@router.get("/s3-inventory")
async def list_s3_inventory(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    orphans_only: bool = Query(default=False),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """S3 inventory objects with table mapping."""
    stmt = select(S3InventoryObject)
    if orphans_only:
        stmt = stmt.where(S3InventoryObject.is_orphan == True)  # noqa: E712
    stmt = (
        stmt.order_by(S3InventoryObject.size_bytes.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(limit)
    )

    rows = session.exec(stmt).all()
    return [
        {
            "id": str(r.id),
            "bucket": r.bucket,
            "key": r.key,
            "size_bytes": r.size_bytes,
            "last_modified": r.last_modified.isoformat() if r.last_modified else None,
            "storage_class": r.storage_class,
            "matched_table": r.matched_table,
            "matched_table_location": r.matched_table_location,
            "is_orphan": r.is_orphan,
            "tags": r.tags,
        }
        for r in rows
    ]


@router.get("/policies")
async def list_policies(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    """Collected cluster policies."""
    rows = session.exec(
        select(ClusterPolicyRecord)
        .order_by(ClusterPolicyRecord.last_seen_at.desc())  # type: ignore[union-attr]
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "id": str(r.id),
            "policy_id": r.policy_id,
            "name": r.name,
            "description": r.description,
            "definition": r.definition,
            "last_seen_at": r.last_seen_at.isoformat() if r.last_seen_at else None,
        }
        for r in rows
    ]
