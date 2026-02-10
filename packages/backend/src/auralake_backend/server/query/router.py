from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import AnalysisResult
from fastapi import APIRouter, Depends, Query

from auralake_backend.server.deps import get_context

from .service import QueryService

router = APIRouter()


@router.get("/analyze")
def analyze(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Analyze queries for anti-patterns and optimization opportunities."""
    return QueryService(context).analyze()


@router.get("/expensive")
def expensive(
    context: Annotated[ExecutionContext, Depends(get_context)],
    days: int = Query(default=30, ge=1, description="Number of days to look back"),
    top_n: int = Query(default=20, ge=1, description="Number of top queries to return"),
) -> list[dict]:
    """Return the most expensive queries over the given period."""
    return QueryService(context).expensive(days=days, top_n=top_n)


@router.get("/plans")
def plans(
    context: Annotated[ExecutionContext, Depends(get_context)],
    workspace: str | None = Query(default=None, description="Filter by workspace"),
) -> list[dict]:
    """List stored Spark query plans."""
    return QueryService(context).plans(workspace=workspace)
