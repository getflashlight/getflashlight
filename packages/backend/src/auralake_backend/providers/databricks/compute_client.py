"""Databricks compute client using the Databricks SDK clusters API."""

from __future__ import annotations

from typing import Any

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.compute import ClusterInfo, ClusterUtilization
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractComputeClient

from auralake_backend.providers.databricks.auth import get_workspace_client


class DatabricksComputeClient(AbstractComputeClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def list_clusters(self) -> list[ClusterInfo]:
        try:
            clusters = self._client.clusters.list()
            return [self._to_cluster_info(c) for c in clusters]
        except Exception as exc:
            raise APIError("databricks", f"Failed to list clusters: {exc}") from exc

    def get_cluster(self, cluster_id: str) -> ClusterInfo:
        try:
            c = self._client.clusters.get(cluster_id)
            return self._to_cluster_info(c)
        except Exception as exc:
            raise APIError("databricks", f"Failed to get cluster {cluster_id}: {exc}") from exc

    def get_utilization(self, cluster_id: str, days: int = 30) -> ClusterUtilization:
        # Utilization requires querying system tables or Ganglia metrics.
        # This is a structured placeholder that returns empty utilization.
        return ClusterUtilization(cluster_id=cluster_id, period_days=days)

    def resize(self, cluster_id: str, config: dict[str, Any]) -> None:
        try:
            self._client.clusters.edit(
                cluster_id=cluster_id,
                **config,
            )
        except Exception as exc:
            raise APIError(
                "databricks",
                f"Failed to resize cluster {cluster_id}: {exc}",
            ) from exc

    def terminate(self, cluster_id: str) -> None:
        try:
            self._client.clusters.delete(cluster_id=cluster_id)
        except Exception as exc:
            raise APIError(
                "databricks",
                f"Failed to terminate cluster {cluster_id}: {exc}",
            ) from exc

    @staticmethod
    def _to_cluster_info(c) -> ClusterInfo:
        autoscale = c.autoscale is not None
        return ClusterInfo(
            cluster_id=c.cluster_id or "",
            cluster_name=c.cluster_name or "",
            state=str(c.state.value) if c.state else "UNKNOWN",
            driver_node_type=c.driver_node_type_id,
            worker_node_type=c.node_type_id,
            num_workers=c.num_workers or 0,
            min_workers=c.autoscale.min_workers if autoscale else None,
            max_workers=c.autoscale.max_workers if autoscale else None,
            autoscale=autoscale,
            autotermination_minutes=c.autotermination_minutes,
            cluster_source=(str(c.cluster_source.value) if c.cluster_source else None),
            creator=c.creator_user_name,
            tags=dict(c.custom_tags) if c.custom_tags else {},
        )
