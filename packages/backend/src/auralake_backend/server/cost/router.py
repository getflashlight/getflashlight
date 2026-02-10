from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import AnalysisResult
from fastapi import APIRouter, Depends, Query

from auralake_backend.server.deps import get_context

from .service import CostService

router = APIRouter()


@router.get("/report")
def get_report(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Run cost analysis and return the full report."""
    return CostService(context).get_report()


@router.get("/breakdown")
def get_breakdown(
    context: Annotated[ExecutionContext, Depends(get_context)],
    days: int = Query(default=30, ge=1, description="Number of days to look back"),
    by: str = Query(default="sku", description="Breakdown dimension"),
) -> dict:
    """Return cost breakdown for the given period."""
    return CostService(context).get_breakdown(days=days)


@router.get("/tco")
def get_tco(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Return total-cost-of-ownership analysis."""
    return CostService(context).get_tco()


@router.get("/infra")
def get_infra(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Return infrastructure cost analysis."""
    return CostService(context).get_infra()
