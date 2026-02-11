"""Read-only data view endpoints for collected data."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, func, select

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import (
    ApiKey,
    BillingRecord,
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


@router.get("/billing")
async def billing_summary(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> dict:
    """Billing summary — total cost and breakdown by SKU."""
    total = session.exec(select(func.sum(BillingRecord.cost_usd))).one() or 0.0

    by_sku_rows = session.exec(
        select(BillingRecord.sku, func.sum(BillingRecord.cost_usd))
        .group_by(BillingRecord.sku)
        .order_by(func.sum(BillingRecord.cost_usd).desc())
    ).all()

    return {
        "total_cost_usd": float(total),
        "by_sku": {row[0]: float(row[1]) for row in by_sku_rows},
    }


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
