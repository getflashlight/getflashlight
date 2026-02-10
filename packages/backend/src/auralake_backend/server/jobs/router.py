from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .service import JobService

router = APIRouter()


@router.get("/analyze")
def analyze(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Analyze jobs for optimization opportunities."""
    return JobService(context).analyze()


@router.get("/stale")
def stale(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Find stale or unused jobs."""
    return JobService(context).stale()


@router.get("/recommend")
def recommend(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Get job optimization recommendations."""
    return JobService(context).recommend()


@router.post("/consolidate")
def consolidate(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[ActionResult]:
    """Consolidate jobs that can share compute resources."""
    return JobService(context).consolidate()
