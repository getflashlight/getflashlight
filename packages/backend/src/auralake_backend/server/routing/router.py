from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import AnalysisResult
from fastapi import APIRouter, Depends, Query

from auralake_backend.server.deps import get_context

from .service import RoutingService

router = APIRouter()


@router.get("/analyze")
def analyze(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Analyze workload portability across providers."""
    return RoutingService(context).analyze()


@router.get("/compare")
def compare(
    context: Annotated[ExecutionContext, Depends(get_context)],
    target_provider: str = Query(description="Provider to compare against"),
) -> dict:
    """Compare current workloads against a target provider."""
    return RoutingService(context).compare(target_provider=target_provider)
