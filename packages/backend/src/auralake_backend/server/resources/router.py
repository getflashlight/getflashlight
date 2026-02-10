from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .schemas import CleanupParams
from .service import ResourceService

router = APIRouter()


@router.get("/scan")
def scan(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Scan for idle resources."""
    return ResourceService(context).scan()


@router.get("/report")
def report(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Generate idle resource report."""
    return ResourceService(context).report()


@router.post("/cleanup")
def cleanup(
    body: CleanupParams,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[ActionResult]:
    """Clean up idle resources by terminating them."""
    return ResourceService(context).cleanup(resource_type=body.resource_type)
