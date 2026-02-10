from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends, Query

from auralake_backend.server.deps import get_context

from .schemas import (
    S3InventoryCollectResponse,
    S3InventoryObjectResponse,
    S3InventoryStatusResponse,
)
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


@router.get("/s3-inventory")
def s3_inventory_status(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> S3InventoryStatusResponse:
    """Get S3 inventory collection status and summary."""
    return TagService(context).s3_inventory_status()


@router.post("/s3-inventory/collect")
def s3_inventory_collect(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> S3InventoryCollectResponse:
    """Trigger on-demand processing of existing S3 inventory reports.

    Idempotent — configures inventory if not already set up,
    then checks for and processes any available reports.
    Returns count of objects ingested + matched.
    """
    return TagService(context).s3_inventory_collect()


@router.get("/s3-report")
def s3_tag_report(
    context: Annotated[ExecutionContext, Depends(get_context)],
    orphans_only: bool = Query(default=False),
) -> list[S3InventoryObjectResponse]:
    """List S3 objects with their table mapping status."""
    return TagService(context).s3_tag_report(orphans_only=orphans_only)
