from __future__ import annotations

from typing import Annotated

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult
from fastapi import APIRouter, Depends

from auralake_backend.server.deps import get_context

from .schemas import ResizeRequest
from .service import ClusterService

router = APIRouter()


@router.get("/analyze")
def analyze(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> AnalysisResult:
    """Run cluster analysis and return recommendations."""
    return ClusterService(context).analyze()


@router.get("/list")
def list_clusters(
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> list[dict]:
    """List all clusters visible to the current provider."""
    return ClusterService(context).list_clusters()


@router.get("/{cluster_id}")
def get_cluster(
    cluster_id: str,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> dict:
    """Get details of a specific cluster."""
    return ClusterService(context).get_cluster(cluster_id)


@router.post("/{cluster_id}/resize")
def resize(
    cluster_id: str,
    body: ResizeRequest,
    context: Annotated[ExecutionContext, Depends(get_context)],
) -> ActionResult:
    """Resize a cluster by changing worker count or instance type."""
    return ClusterService(context).resize(
        cluster_id,
        workers=body.workers,
        instance_type=body.instance_type,
    )
