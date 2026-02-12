"""Chat tool functions that query the DB and return dicts.

Each tool also has an entry in TOOL_DEFINITIONS (OpenAI function-calling format).
All tools are read-only (SELECT only) and receive a SQLModel Session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlmodel import Session, func, select

from auralake_backend.db.models import (
    BillingRecord,
    ComputeResourceRecord,
    JobProfileRecord,
    JobRunRecord,
    RecommendationRecord,
    S3InventoryObject,
    UnityCatalogTableRecord,
)

# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


def get_cost_summary(session: Session, days: int = 30) -> dict[str, Any]:
    """Total spend, DBU usage, top SKUs/clusters/jobs by cost, daily trend."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    total = (
        session.exec(
            select(func.sum(BillingRecord.cost_usd)).where(
                BillingRecord.usage_date >= cutoff.date()
            )
        ).one()
        or 0.0
    )

    total_dbu = (
        session.exec(
            select(func.sum(BillingRecord.dbu_usage)).where(
                BillingRecord.usage_date >= cutoff.date()
            )
        ).one()
        or 0.0
    )

    by_sku = session.exec(
        select(
            BillingRecord.sku,
            func.sum(BillingRecord.cost_usd),
            func.sum(BillingRecord.dbu_usage),
        )
        .where(BillingRecord.usage_date >= cutoff.date())
        .group_by(BillingRecord.sku)
        .order_by(func.sum(BillingRecord.dbu_usage).desc())
        .limit(10)
    ).all()

    by_cluster = session.exec(
        select(BillingRecord.cluster_id, func.sum(BillingRecord.cost_usd))
        .where(
            BillingRecord.usage_date >= cutoff.date(),
            BillingRecord.cluster_id.is_not(None),  # type: ignore[union-attr]
        )
        .group_by(BillingRecord.cluster_id)
        .order_by(func.sum(BillingRecord.cost_usd).desc())
        .limit(10)
    ).all()

    by_job = session.exec(
        select(BillingRecord.job_id, func.sum(BillingRecord.cost_usd))
        .where(
            BillingRecord.usage_date >= cutoff.date(),
            BillingRecord.job_id.is_not(None),  # type: ignore[union-attr]
        )
        .group_by(BillingRecord.job_id)
        .order_by(func.sum(BillingRecord.cost_usd).desc())
        .limit(10)
    ).all()

    daily = session.exec(
        select(BillingRecord.usage_date, func.sum(BillingRecord.cost_usd))
        .where(BillingRecord.usage_date >= cutoff.date())
        .group_by(BillingRecord.usage_date)
        .order_by(BillingRecord.usage_date)
    ).all()

    return {
        "period_days": days,
        "total_cost_usd": float(total),
        "total_dbu_usage": float(total_dbu),
        "by_sku": [
            {"sku": r[0], "cost_usd": float(r[1]), "dbu_usage": float(r[2])} for r in by_sku
        ],
        "by_cluster": [{"cluster_id": r[0], "cost_usd": float(r[1])} for r in by_cluster],
        "by_job": [{"job_id": r[0], "cost_usd": float(r[1])} for r in by_job],
        "daily_trend": [{"date": r[0].isoformat(), "cost_usd": float(r[1])} for r in daily],
    }


def get_recommendations(
    session: Session,
    type_filter: str | None = None,
    risk_level: str | None = None,
    status: str | None = None,
    min_savings_usd: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Optimization recommendations with savings estimates."""
    stmt = select(RecommendationRecord)
    if type_filter:
        stmt = stmt.where(RecommendationRecord.type == type_filter)
    if risk_level:
        stmt = stmt.where(RecommendationRecord.risk_level == risk_level)
    if status:
        stmt = stmt.where(RecommendationRecord.status == status)
    if min_savings_usd is not None:
        stmt = stmt.where(RecommendationRecord.estimated_monthly_savings_usd >= min_savings_usd)

    total_count = session.exec(select(func.count()).select_from(stmt.subquery())).one()

    rows = session.exec(
        stmt.order_by(
            RecommendationRecord.estimated_monthly_savings_usd.desc()  # type: ignore[union-attr]
        ).limit(limit)
    ).all()

    return {
        "total_count": total_count,
        "showing": len(rows),
        "recommendations": [
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
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


def get_compute_resources(
    session: Session,
    resource_type: str | None = None,
    state: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Clusters/warehouses with config and state."""
    stmt = select(ComputeResourceRecord)
    if resource_type:
        stmt = stmt.where(ComputeResourceRecord.resource_type == resource_type)
    if state:
        stmt = stmt.where(ComputeResourceRecord.state == state)

    total_count = session.exec(select(func.count()).select_from(stmt.subquery())).one()

    rows = session.exec(
        stmt.order_by(
            ComputeResourceRecord.last_seen_at.desc()  # type: ignore[union-attr]
        ).limit(limit)
    ).all()

    return {
        "total_count": total_count,
        "showing": len(rows),
        "resources": [
            {
                "resource_id": r.resource_id,
                "resource_name": r.resource_name,
                "resource_type": r.resource_type,
                "state": r.state,
                "creator": r.creator,
                "driver_node_type": r.driver_node_type,
                "worker_node_type": r.worker_node_type,
                "num_workers": r.num_workers,
                "min_workers": r.min_workers,
                "max_workers": r.max_workers,
                "autoscale": r.autoscale,
                "spot_enabled": r.spot_enabled,
                "autotermination_minutes": r.autotermination_minutes,
                "warehouse_type": r.warehouse_type,
                "warehouse_size": r.warehouse_size,
                "last_seen_at": (r.last_seen_at.isoformat() if r.last_seen_at else None),
            }
            for r in rows
        ],
    }


def get_job_status(
    session: Session,
    state_filter: str | None = None,
    days: int = 7,
    sort_by: str = "cost",
    limit: int = 20,
) -> dict[str, Any]:
    """Jobs with recent run stats (failure rate, cost)."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    # Get job profiles
    jobs_stmt = select(JobProfileRecord)
    total_count = session.exec(select(func.count()).select_from(JobProfileRecord)).one()
    jobs = session.exec(
        jobs_stmt.order_by(
            JobProfileRecord.avg_dbu_cost.desc()
            if sort_by == "cost"
            else JobProfileRecord.last_analyzed_at.desc()  # type: ignore[union-attr]
        ).limit(limit)
    ).all()

    results = []
    for job in jobs:
        # Get run stats for this job in the time window
        runs = session.exec(
            select(JobRunRecord).where(
                JobRunRecord.job_id == job.job_id,
                JobRunRecord.start_time >= cutoff,  # type: ignore[operator]
            )
        ).all()

        total_runs = len(runs)
        failed_runs = sum(1 for r in runs if r.state == "FAILED")

        if state_filter:
            matching = [r for r in runs if r.state == state_filter]
            if not matching and state_filter == "FAILED" and failed_runs == 0:
                continue

        results.append(
            {
                "job_id": job.job_id,
                "job_name": job.job_name,
                "schedule_cron": job.schedule_cron,
                "avg_duration_minutes": job.avg_duration_minutes,
                "avg_dbu_cost": job.avg_dbu_cost,
                "instance_type": job.instance_type,
                "worker_count": job.worker_count,
                "recent_runs": total_runs,
                "recent_failures": failed_runs,
                "failure_rate": (round(failed_runs / total_runs, 3) if total_runs > 0 else 0),
            }
        )

    return {
        "period_days": days,
        "total_jobs": total_count,
        "showing": len(results),
        "jobs": results,
    }


def get_table_maintenance(
    session: Session,
    needs_vacuum: bool | None = None,
    needs_optimize: bool | None = None,
    min_size_gb: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Delta tables needing maintenance."""
    stmt = select(UnityCatalogTableRecord)
    if needs_vacuum is True:
        stmt = stmt.where(UnityCatalogTableRecord.last_vacuumed_at.is_(None))  # type: ignore[union-attr]
    if needs_optimize is True:
        stmt = stmt.where(UnityCatalogTableRecord.last_optimized_at.is_(None))  # type: ignore[union-attr]
    if min_size_gb is not None:
        min_bytes = int(min_size_gb * 1_073_741_824)
        stmt = stmt.where(UnityCatalogTableRecord.size_bytes >= min_bytes)  # type: ignore[operator]

    total_count = session.exec(select(func.count()).select_from(stmt.subquery())).one()

    rows = session.exec(
        stmt.order_by(
            UnityCatalogTableRecord.size_bytes.desc()  # type: ignore[union-attr]
        ).limit(limit)
    ).all()

    return {
        "total_count": total_count,
        "showing": len(rows),
        "tables": [
            {
                "full_name": r.full_name,
                "table_type": r.table_type,
                "data_format": r.data_format,
                "size_bytes": r.size_bytes,
                "size_gb": round(r.size_bytes / 1_073_741_824, 2) if r.size_bytes else None,
                "num_files": r.num_files,
                "last_optimized_at": (
                    r.last_optimized_at.isoformat() if r.last_optimized_at else None
                ),
                "last_vacuumed_at": (
                    r.last_vacuumed_at.isoformat() if r.last_vacuumed_at else None
                ),
                "optimize_count_30d": r.optimize_count_30d,
                "vacuum_count_30d": r.vacuum_count_30d,
                "uses_liquid_clustering": r.uses_liquid_clustering,
                "uses_zordering": r.uses_zordering,
            }
            for r in rows
        ],
    }


def get_storage_analysis(
    session: Session,
    orphans_only: bool = False,
    min_size_mb: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """S3 objects, orphan detection."""
    stmt = select(S3InventoryObject)
    if orphans_only:
        stmt = stmt.where(S3InventoryObject.is_orphan == True)  # noqa: E712
    if min_size_mb is not None:
        min_bytes = int(min_size_mb * 1_048_576)
        stmt = stmt.where(S3InventoryObject.size_bytes >= min_bytes)

    total_count = session.exec(select(func.count()).select_from(stmt.subquery())).one()

    rows = session.exec(
        stmt.order_by(S3InventoryObject.size_bytes.desc()).limit(limit)  # type: ignore[union-attr]
    ).all()

    return {
        "total_count": total_count,
        "showing": len(rows),
        "objects": [
            {
                "bucket": r.bucket,
                "key": r.key,
                "size_bytes": r.size_bytes,
                "size_mb": round(r.size_bytes / 1_048_576, 2),
                "storage_class": r.storage_class,
                "is_orphan": r.is_orphan,
                "matched_table": r.matched_table,
                "last_modified": (r.last_modified.isoformat() if r.last_modified else None),
            }
            for r in rows
        ],
    }


def get_data_summary(session: Session) -> dict[str, Any]:
    """Record counts, total billing, recommendation counts by status."""
    billing_count = session.exec(select(func.count()).select_from(BillingRecord)).one()
    total_cost = session.exec(select(func.sum(BillingRecord.cost_usd))).one() or 0.0
    cluster_count = session.exec(
        select(func.count()).select_from(
            select(ComputeResourceRecord)
            .where(
                ComputeResourceRecord.resource_type.in_(  # type: ignore[union-attr]
                    ["all_purpose_cluster", "job_cluster"]
                )
            )
            .subquery()
        )
    ).one()
    warehouse_count = session.exec(
        select(func.count()).select_from(
            select(ComputeResourceRecord)
            .where(ComputeResourceRecord.resource_type == "sql_warehouse")
            .subquery()
        )
    ).one()
    job_count = session.exec(select(func.count()).select_from(JobProfileRecord)).one()
    rec_count = session.exec(select(func.count()).select_from(RecommendationRecord)).one()
    table_count = session.exec(select(func.count()).select_from(UnityCatalogTableRecord)).one()
    s3_count = session.exec(select(func.count()).select_from(S3InventoryObject)).one()

    # Recommendation breakdown by status
    rec_by_status = session.exec(
        select(RecommendationRecord.status, func.count()).group_by(RecommendationRecord.status)
    ).all()

    total_savings = (
        session.exec(select(func.sum(RecommendationRecord.estimated_monthly_savings_usd))).one()
        or 0.0
    )

    return {
        "billing_records": billing_count,
        "total_cost_usd": float(total_cost),
        "clusters": cluster_count,
        "warehouses": warehouse_count,
        "jobs": job_count,
        "recommendations": rec_count,
        "recommendations_by_status": {r[0]: r[1] for r in rec_by_status},
        "total_estimated_savings_usd": float(total_savings),
        "unity_catalog_tables": table_count,
        "s3_inventory_objects": s3_count,
    }


def create_visualization(
    chart_type: str,
    title: str,
    data: dict[str, list[Any]],
    x: str,
    y: str | list[str],
) -> dict[str, Any]:
    """Capture a chart specification. Returns confirmation; actual chart is
    extracted by the service layer and included in ChatResponse.charts."""
    return {
        "status": "chart_created",
        "chart_type": chart_type,
        "title": title,
    }


# ---------------------------------------------------------------------------
# Tool dispatch map
# ---------------------------------------------------------------------------

TOOL_DISPATCH: dict[str, Any] = {
    "get_cost_summary": get_cost_summary,
    "get_recommendations": get_recommendations,
    "get_compute_resources": get_compute_resources,
    "get_job_status": get_job_status,
    "get_table_maintenance": get_table_maintenance,
    "get_storage_analysis": get_storage_analysis,
    "get_data_summary": get_data_summary,
    "create_visualization": create_visualization,
}


# ---------------------------------------------------------------------------
# OpenAI function-calling tool definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_cost_summary",
            "description": (
                "Get total spend, top SKUs/clusters/jobs by cost, and daily "
                "cost trend for the specified period."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (default 30).",
                        "default": 30,
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recommendations",
            "description": (
                "Get cost optimization recommendations with savings estimates. "
                "Filter by type, risk level, status, or minimum savings amount."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type_filter": {
                        "type": "string",
                        "description": "Filter by recommendation type.",
                    },
                    "risk_level": {
                        "type": "string",
                        "description": "Filter by risk level (low/medium/high).",
                    },
                    "status": {
                        "type": "string",
                        "description": ("Filter by status (pending/applied/dismissed/pr_created)."),
                    },
                    "min_savings_usd": {
                        "type": "number",
                        "description": "Minimum estimated monthly savings in USD.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 20).",
                        "default": 20,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_compute_resources",
            "description": (
                "List compute resources (clusters, warehouses) with their "
                "configuration and current state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_type": {
                        "type": "string",
                        "description": (
                            "Filter by type: all_purpose_cluster, job_cluster, sql_warehouse."
                        ),
                    },
                    "state": {
                        "type": "string",
                        "description": ("Filter by state: RUNNING, TERMINATED, STOPPED, etc."),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 50).",
                        "default": 50,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_job_status",
            "description": (
                "Get job profiles with recent run statistics including failure rate and cost."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "state_filter": {
                        "type": "string",
                        "description": ("Filter by run state (SUCCESS/FAILED/TIMEDOUT/CANCELLED)."),
                    },
                    "days": {
                        "type": "integer",
                        "description": "Days of run history to include (default 7).",
                        "default": 7,
                    },
                    "sort_by": {
                        "type": "string",
                        "description": "Sort by 'cost' or 'recent' (default 'cost').",
                        "default": "cost",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                        "default": 20,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_table_maintenance",
            "description": (
                "List Delta/Unity Catalog tables with maintenance status. "
                "Find tables that need VACUUM or OPTIMIZE."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "needs_vacuum": {
                        "type": "boolean",
                        "description": "Only show tables that have never been vacuumed.",
                    },
                    "needs_optimize": {
                        "type": "boolean",
                        "description": ("Only show tables that have never been optimized."),
                    },
                    "min_size_gb": {
                        "type": "number",
                        "description": "Minimum table size in GB.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                        "default": 20,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_storage_analysis",
            "description": (
                "Analyze S3 storage objects. Find orphaned files not linked to any table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "orphans_only": {
                        "type": "boolean",
                        "description": "Only show orphan objects (default false).",
                        "default": False,
                    },
                    "min_size_mb": {
                        "type": "number",
                        "description": "Minimum object size in MB.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                        "default": 20,
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_data_summary",
            "description": (
                "Get a high-level summary of all collected data: record counts, "
                "total billing, recommendations by status, total potential savings."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_visualization",
            "description": (
                "Create a chart/visualization from data. Call this when you want "
                "to display a chart to the user. Specify the chart type, title, "
                "data columns, and x/y axes. The chart will be rendered by the UI."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "line", "pie", "area", "heatmap", "scatter", "histogram"],
                        "description": "Type of chart to create.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Chart title.",
                    },
                    "data": {
                        "type": "object",
                        "description": (
                            "Chart data as column_name → list of values. "
                            'E.g. {"sku": ["A","B"], "cost": [100,200]}'
                        ),
                        "additionalProperties": {"type": "array", "items": {}},
                    },
                    "x": {
                        "type": "string",
                        "description": "Column name for x-axis.",
                    },
                    "y": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Column name(s) for y-axis.",
                    },
                },
                "required": ["chart_type", "title", "data", "x", "y"],
            },
        },
    },
]
