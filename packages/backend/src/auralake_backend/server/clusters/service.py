from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.recommendations import ActionResult, AnalysisResult


class ClusterService:
    def __init__(self, context: ExecutionContext) -> None:
        self.context = context

    def analyze(self) -> AnalysisResult:
        from auralake_backend.analyzers.cluster_analyzer import ClusterAnalyzer

        return ClusterAnalyzer(self.context).analyze()

    def list_clusters(self) -> list[dict]:
        compute_client = self.context.provider.get_compute_client()
        clusters = compute_client.list_clusters()
        return [c.model_dump() for c in clusters]

    def get_cluster(self, cluster_id: str) -> dict:
        compute_client = self.context.provider.get_compute_client()
        cluster = compute_client.get_cluster(cluster_id)
        return cluster.model_dump()

    def resize(
        self,
        cluster_id: str,
        workers: int | None,
        instance_type: str | None,
    ) -> ActionResult:
        compute_client = self.context.provider.get_compute_client()
        changes: dict[str, object] = {}
        if workers is not None:
            changes["num_workers"] = workers
        if instance_type is not None:
            changes["instance_type"] = instance_type
        compute_client.resize(cluster_id, changes)
        return ActionResult(
            action_type="resize_cluster",
            resource_id=cluster_id,
            resource_name=cluster_id,
            status="applied",
            detail=f"Resized cluster with changes: {changes}",
        )
