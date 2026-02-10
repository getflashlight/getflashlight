from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .schemas import SpotApplyRequest
from .service import SpotService

router = APIRouter()


@router.get("/analyze")
def analyze(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Analyze spot instance usage and opportunities."""
    return SpotService(context).analyze()


@router.get("/recommend")
def recommend(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Get spot optimization recommendations."""
    return SpotService(context).recommend()


@router.post("/apply")
def apply(
    body: SpotApplyRequest,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[ActionResult]:
    """Apply spot optimizations, optionally targeting a specific cluster."""
    return SpotService(context).apply(cluster_id=body.cluster_id)
