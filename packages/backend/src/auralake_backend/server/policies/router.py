from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .service import PolicyService

router = APIRouter()


@router.get("/audit")
def audit(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Audit cluster policies for compliance issues."""
    return PolicyService(context).audit()


@router.get("/recommend")
def recommend(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Get policy recommendations."""
    return PolicyService(context).recommend()


@router.post("/apply")
def apply(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[ActionResult]:
    """Apply recommended policy changes (e.g. set autotermination)."""
    return PolicyService(context).apply()
