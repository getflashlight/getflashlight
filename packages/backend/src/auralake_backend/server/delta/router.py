from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .schemas import OptimizeRequest, VacuumRequest
from .service import DeltaService

router = APIRouter()


@router.get("/scan")
def scan(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Scan Delta tables for maintenance opportunities."""
    return DeltaService(context).scan()


@router.post("/optimize")
def optimize(
    body: OptimizeRequest,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> ActionResult:
    """Run OPTIMIZE on a Delta table."""
    return DeltaService(context).optimize(table=body.table)


@router.post("/vacuum")
def vacuum(
    body: VacuumRequest,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> ActionResult:
    """Run VACUUM on a Delta table."""
    return DeltaService(context).vacuum(
        table=body.table,
        retention_hours=body.retention_hours,
    )
