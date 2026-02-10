from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .service import AgentService

router = APIRouter()


@router.get("/status")
def status(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> dict:
    """Get the current collector agent status."""
    return AgentService(context).status()


@router.post("/start")
def start(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> dict:
    """Request the collector agent to start."""
    return AgentService(context).start()


@router.post("/stop")
def stop(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> dict:
    """Request the collector agent to stop."""
    return AgentService(context).stop()
