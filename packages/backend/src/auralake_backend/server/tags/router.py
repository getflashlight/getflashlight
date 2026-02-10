from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .service import TagService

router = APIRouter()


@router.get("/scan")
def scan(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Scan for tag policy violations."""
    return TagService(context).scan()


@router.get("/report")
def report(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Generate tag compliance report."""
    return TagService(context).report()


@router.post("/enforce")
def enforce(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[ActionResult]:
    """Enforce tag policies on non-compliant resources."""
    return TagService(context).enforce()
