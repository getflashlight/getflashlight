"""Collection agent control-plane endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from auralake_backend.db.models import ApiKey
from auralake_backend.server.auth import require_auth

router = APIRouter()


def _get_task_manager(request: Request):
    tm = getattr(request.app.state, "task_manager", None)
    if tm is None:
        raise HTTPException(status_code=503, detail="Task manager not initialized")
    return tm


def _get_config(request: Request):
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="Server not configured")
    return config


@router.get("/status")
async def overall_status(
    _auth: ApiKey = Depends(require_auth),
    tm=Depends(_get_task_manager),
) -> dict:
    """Overall collection status — active tasks."""
    return {"active": tm.list_active()}


@router.get("/status/{connection_id}")
async def connection_status(
    connection_id: uuid.UUID,
    _auth: ApiKey = Depends(require_auth),
    tm=Depends(_get_task_manager),
) -> dict:
    """Per-connection collection status with per-worker details."""
    status = tm.get_status(connection_id)
    if status is None:
        raise HTTPException(status_code=404, detail="No collection runs found")
    return status


@router.post("/collect/{connection_id}")
async def trigger_collection(
    connection_id: uuid.UUID,
    _auth: ApiKey = Depends(require_auth),
    tm=Depends(_get_task_manager),
    config=Depends(_get_config),
) -> dict:
    """Manually trigger a full collection for a connection."""
    try:
        run = tm.start_collection(connection_id, config, trigger="manual")
        return {"status": "started", "run_id": str(run.id)}
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/retry/{connection_id}/{worker_name}")
async def retry_worker(
    connection_id: uuid.UUID,
    worker_name: str,
    _auth: ApiKey = Depends(require_auth),
    tm=Depends(_get_task_manager),
    config=Depends(_get_config),
) -> dict:
    """Retry a single failed worker for a connection."""
    run = tm.retry_worker(connection_id, worker_name, config)
    if run is None:
        raise HTTPException(status_code=404, detail="No collection run found")
    return {"status": "retrying", "worker": worker_name, "run_id": str(run.id)}


@router.post("/cancel/{connection_id}")
async def cancel_collection(
    connection_id: uuid.UUID,
    _auth: ApiKey = Depends(require_auth),
    tm=Depends(_get_task_manager),
) -> dict:
    """Cancel a running collection."""
    cancelled = tm.cancel_collection(connection_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="No active collection found")
    return {"status": "cancelling"}


@router.get("/history")
async def collection_history(
    _auth: ApiKey = Depends(require_auth),
    tm=Depends(_get_task_manager),
) -> list[dict]:
    """List past collection runs."""
    return tm.get_history()
