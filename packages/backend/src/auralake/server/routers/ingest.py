"""Ingest + transform triggers and run history."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlmodel import desc, select

from auralake.ingest.base import IngestWindow
from auralake.ingest.config import load_connections
from auralake.ingest.runner import DEFAULT_LOOKBACK_DAYS, build_connector, run_connector
from auralake.server.auth import require_api_key
from auralake.store.engine import session_scope
from auralake.store.models import IngestRun
from auralake.transform.runner import apply_views

router = APIRouter(prefix="/api/v1", tags=["ingest"], dependencies=[Depends(require_api_key)])


class IngestRequest(BaseModel):
    start: date | None = None
    end: date | None = None
    connections_path: str | None = None
    run_transform: bool = True


@router.post("/ingest")
def trigger_ingest(req: IngestRequest) -> dict[str, Any]:
    """Run all enabled connectors synchronously, then refresh the views."""
    end = req.end or date.today()
    start = req.start or (end - timedelta(days=DEFAULT_LOOKBACK_DAYS))
    window = IngestWindow(start=start, end=end)

    results: dict[str, int] = {}
    for config in load_connections(req.connections_path):
        connector = build_connector(config)
        results[connector.name] = run_connector(connector, window)

    statements = apply_views() if req.run_transform else 0
    return {
        "window": {"start": str(start), "end": str(end)},
        "rows_by_connector": results,
        "transform_statements": statements,
    }


@router.get("/runs")
def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Recent ingest runs, newest first."""
    with session_scope() as session:
        rows = session.exec(
            select(IngestRun).order_by(desc(IngestRun.id)).limit(limit)
        ).all()
        return [r.model_dump() for r in rows]
