"""Databricks compute client using the Databricks SDK clusters API."""

from __future__ import annotations

from typing import Any

import structlog
from auralake_shared.core.exceptions import APIError
from auralake_shared.models.compute import ClusterInfo, ClusterUtilization
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractComputeClient

from auralake_backend.providers.databricks.auth import get_workspace_client

logger = structlog.get_logger(__name__)


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
        """Query cluster utilization from Databricks system tables.

        Uses ``system.compute.cluster_event_log`` to derive active/idle hours
        and ``system.billing.usage`` to get DBU cost attributed to this cluster.
        Falls back to an empty utilization record if system tables are
        unavailable (requires Unity Catalog + Premium/Enterprise tier).
        """
        try:
            utilization_rows = self._execute_sql(f"""
                SELECT
                    cluster_id,
                    COUNT(DISTINCT DATE(timestamp)) AS active_days,
                    SUM(CASE WHEN type = 'RUNNING' THEN
                        UNIX_TIMESTAMP(COALESCE(LEAD(timestamp) OVER (
                            PARTITION BY cluster_id ORDER BY timestamp
                        ), CURRENT_TIMESTAMP())) - UNIX_TIMESTAMP(timestamp)
                    ELSE 0 END) / 3600.0 AS active_hours,
                    SUM(CASE WHEN type IN ('RESIZING', 'UPSIZE_COMPLETED') THEN 0
                        WHEN type = 'RUNNING' THEN 0
                        ELSE UNIX_TIMESTAMP(COALESCE(LEAD(timestamp) OVER (
                            PARTITION BY cluster_id ORDER BY timestamp
                        ), CURRENT_TIMESTAMP())) - UNIX_TIMESTAMP(timestamp)
                    END) / 3600.0 AS idle_hours
                FROM system.compute.cluster_event_log
                WHERE cluster_id = '{cluster_id}'
                  AND timestamp >= CURRENT_TIMESTAMP() - INTERVAL {days} DAYS
                GROUP BY cluster_id
            """)

            active_hours = 0.0
            idle_hours = 0.0
            if utilization_rows:
                row = utilization_rows[0]
                active_hours = float(row.get("active_hours") or 0)
                idle_hours = float(row.get("idle_hours") or 0)

            # Query billing for cost + DBU attributed to this cluster
            cost_rows = self._execute_sql(f"""
                SELECT
                    SUM(usage_quantity) AS total_dbu,
                    SUM(usage_quantity * pricing.default.effective_list.default) AS total_cost_usd
                FROM system.billing.usage
                WHERE usage_metadata.cluster_id = '{cluster_id}'
                  AND usage_date >= CURRENT_DATE() - INTERVAL {days} DAYS
            """)

            total_dbu = 0.0
            total_cost = 0.0
            if cost_rows:
                row = cost_rows[0]
                total_dbu = float(row.get("total_dbu") or 0)
                total_cost = float(row.get("total_cost_usd") or 0)

            return ClusterUtilization(
                cluster_id=cluster_id,
                period_days=days,
                active_hours=active_hours,
                idle_hours=idle_hours,
                total_dbu=total_dbu,
                total_cost_usd=total_cost,
            )
        except Exception as exc:
            logger.warning(
                "utilization_query_failed",
                cluster_id=cluster_id,
                error=str(exc),
                hint="System tables require Unity Catalog + Premium/Enterprise tier.",
            )
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

    def _execute_sql(self, sql: str) -> list[dict]:
        """Execute SQL via a workspace with a SQL warehouse configured."""
        for ws_name, ws_config in self._config.workspaces.items():
            if ws_config.sql_warehouse_id:
                client = get_workspace_client(self._config, ws_name)
                result = client.statement_execution.execute_statement(
                    warehouse_id=ws_config.sql_warehouse_id,
                    statement=sql,
                )
                if result.result and result.result.data_array:
                    columns = [col.name for col in (result.manifest.schema.columns or [])]
                    return [dict(zip(columns, row)) for row in result.result.data_array]
                return []
        raise APIError("databricks", "No workspace with sql_warehouse_id configured")

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
