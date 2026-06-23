"""Read API over the GOLD metric views."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from auralake.server.auth import require_api_key
from auralake.store.query import QueryError, query_view
from auralake.transform.catalog import CATALOG

router = APIRouter(prefix="/api/v1", tags=["metrics"], dependencies=[Depends(require_api_key)])


@router.get("/metrics")
def list_metrics() -> list[dict[str, Any]]:
    """List the catalogued GOLD metric views and their schema."""
    return [
        {
            "name": v.name,
            "title": v.title,
            "description": v.description,
            "cost_metric": v.cost_metric.value,
            "dimensions": list(v.dimensions),
            "measures": list(v.measures),
        }
        for v in CATALOG
    ]


@router.get("/metrics/{view_name}")
def get_metric(
    view_name: str,
    limit: int = Query(default=1000, ge=1, le=10_000),
    order_by: str | None = None,
    descending: bool = False,
) -> dict[str, Any]:
    """Return rows from a single GOLD view."""
    full_name = view_name if view_name.startswith("gold.") else f"gold.{view_name}"
    try:
        rows = query_view(full_name, limit=limit, order_by=order_by, descending=descending)
    except QueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"view": full_name, "row_count": len(rows), "rows": rows}
