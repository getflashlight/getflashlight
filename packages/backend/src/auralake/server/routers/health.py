"""Liveness/readiness endpoint."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from auralake import __version__
from auralake.store.engine import get_engine

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    db_ok = "ok"
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = "unavailable"
    return {"status": "ok", "version": __version__, "database": db_ok}
