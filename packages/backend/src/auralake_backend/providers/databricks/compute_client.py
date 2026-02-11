"""Databricks compute client using the Databricks SDK clusters API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog
from auralake_shared.core.exceptions import APIError
from auralake_shared.models.compute import ClusterInfo, ClusterUtilization
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractComputeClient

from auralake_backend.providers.databricks.auth import get_warehouse_id, get_workspace_client

logger = structlog.get_logger(__name__)


class DatabricksComputeClient(AbstractComputeClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def list_clusters(self) -> list[ClusterInfo]:
        from databricks.sdk.service.compute import ListClustersFilterBy, State

        try:
            clusters = self._client.clusters.list(
                page_size=100,
                filter_by=ListClustersFilterBy(
                    cluster_states=[
                        State.RUNNING,
                        State.PENDING,
                        State.RESTARTING,
                        State.RESIZING,
                    ],
                ),
            )
            return [self._to_cluster_info(c) for c in clusters]
        except Exception as exc:
            raise APIError("databricks", f"Failed to list clusters: {exc}") from exc

    def list_all_clusters_with_config(self) -> list[tuple[ClusterInfo, dict[str, Any]]]:
        try:
            clusters = self._client.clusters.list(page_size=100)
            results: list[tuple[ClusterInfo, dict[str, Any]]] = []
            for c in clusters:
                info = self._to_cluster_info(c)
                raw_config: dict[str, Any] = {}
                if c.spark_conf:
                    raw_config["spark_conf"] = dict(c.spark_conf)
                if c.spark_env_vars:
                    raw_config["spark_env_vars"] = dict(c.spark_env_vars)
                if c.policy_id:
                    raw_config["policy_id"] = c.policy_id
                if c.spark_version:
                    raw_config["spark_version"] = c.spark_version
                if c.runtime_engine:
                    raw_config["runtime_engine"] = str(c.runtime_engine.value)
                if c.data_security_mode:
                    raw_config["data_security_mode"] = str(c.data_security_mode.value)
                if getattr(c, "single_user_name", None):
                    raw_config["single_user_name"] = c.single_user_name
                if c.aws_attributes:
                    raw_config["aws_attributes"] = {
                        k: str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v
                        for k, v in c.aws_attributes.as_dict().items()
                    }
                results.append((info, raw_config))
            return results
        except Exception as exc:
            raise APIError("databricks", f"Failed to list all clusters with config: {exc}") from exc

    def list_warehouses(self) -> list[dict[str, Any]]:
        try:
            warehouses = self._client.warehouses.list()
            results: list[dict[str, Any]] = []
            for wh in warehouses:
                tags: dict[str, str] = {}
                if wh.tags and wh.tags.custom_tags:
                    for tag in wh.tags.custom_tags:
                        if tag.key and tag.value:
                            tags[tag.key] = tag.value
                results.append(
                    {
                        "warehouse_id": wh.id or "",
                        "name": wh.name or "",
                        "state": str(wh.state.value) if wh.state else "UNKNOWN",
                        "warehouse_type": str(wh.warehouse_type.value)
                        if wh.warehouse_type
                        else None,
                        "cluster_size": wh.cluster_size or None,
                        "min_num_clusters": wh.min_num_clusters,
                        "max_num_clusters": wh.max_num_clusters,
                        "auto_stop_mins": wh.auto_stop_mins,
                        "creator_name": wh.creator_name,
                        "tags": tags,
                        "spot_instance_policy": (
                            str(wh.spot_instance_policy.value) if wh.spot_instance_policy else None
                        ),
                        "channel": (
                            str(wh.channel.name.value) if wh.channel and wh.channel.name else None
                        ),
                    }
                )
            return results
        except Exception as exc:
            raise APIError("databricks", f"Failed to list warehouses: {exc}") from exc

    def list_pipelines(self) -> list[dict[str, Any]]:
        try:
            pipelines = self._client.pipelines.list_pipelines()
            results: list[dict[str, Any]] = []
            for p in pipelines:
                spec = p.spec
                config: dict[str, Any] = {}
                if spec:
                    if spec.target:
                        config["target"] = spec.target
                    if spec.catalog:
                        config["catalog"] = spec.catalog
                    if spec.channel:
                        config["channel"] = str(spec.channel)
                    config["photon"] = bool(spec.photon)
                    config["serverless"] = bool(spec.serverless)
                    config["continuous"] = bool(spec.continuous)
                    config["development"] = bool(spec.development)
                    if spec.clusters:
                        config["clusters"] = [c.as_dict() for c in spec.clusters if c]
                    if spec.configuration:
                        config["configuration"] = dict(spec.configuration)
                results.append(
                    {
                        "pipeline_id": p.pipeline_id or "",
                        "name": p.name or "",
                        "state": str(p.state.value) if p.state else "UNKNOWN",
                        "creator": getattr(p, "creator_user_name", None),
                        "config": config,
                    }
                )
            return results
        except Exception as exc:
            raise APIError("databricks", f"Failed to list pipelines: {exc}") from exc

    def list_serving_endpoints(self) -> list[dict[str, Any]]:
        try:
            endpoints = self._client.serving_endpoints.list()
            results: list[dict[str, Any]] = []
            for ep in endpoints:
                config: dict[str, Any] = {}
                if ep.config:
                    served = ep.config.served_entities or ep.config.served_models or []
                    config["served_entities"] = [e.as_dict() for e in served if e]
                if getattr(ep, "route_optimized", None) is not None:
                    config["route_optimized"] = ep.route_optimized
                if getattr(ep, "ai_gateway", None):
                    config["ai_gateway"] = ep.ai_gateway.as_dict()

                state_str = "UNKNOWN"
                if ep.state and ep.state.ready:
                    state_str = str(ep.state.ready.value)

                results.append(
                    {
                        "endpoint_name": ep.name or "",
                        "state": state_str,
                        "creator": getattr(ep, "creator", None),
                        "config": config,
                    }
                )
            return results
        except Exception as exc:
            raise APIError("databricks", f"Failed to list serving endpoints: {exc}") from exc

    def list_vector_search_endpoints(self) -> list[dict[str, Any]]:
        try:
            resp = self._client.vector_search_endpoints.list_endpoints()
            # SDK may return a generator (iterable) or an object with .endpoints
            if hasattr(resp, "endpoints"):
                endpoints = resp.endpoints or []
            else:
                endpoints = list(resp) if resp else []
            results: list[dict[str, Any]] = []
            for ep in endpoints:
                config: dict[str, Any] = {}
                if getattr(ep, "endpoint_type", None):
                    config["endpoint_type"] = str(ep.endpoint_type.value)
                if getattr(ep, "num_indexes", None) is not None:
                    config["num_indexes"] = ep.num_indexes

                state_str = "UNKNOWN"
                if ep.endpoint_status and ep.endpoint_status.state:
                    state_str = str(ep.endpoint_status.state.value)

                results.append(
                    {
                        "endpoint_name": ep.name or "",
                        "state": state_str,
                        "creator": getattr(ep, "creator", None),
                        "config": config,
                    }
                )
            return results
        except Exception as exc:
            raise APIError("databricks", f"Failed to list vector search endpoints: {exc}") from exc

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
        """Execute SQL via the first workspace with a reachable warehouse."""
        for ws_name, ws_config in self._config.workspaces.items():
            try:
                client = get_workspace_client(self._config, ws_name)
                wh_id = get_warehouse_id(client, ws_config.sql_warehouse_id)
                result = client.statement_execution.execute_statement(
                    warehouse_id=wh_id,
                    statement=sql,
                )
                if result.result and result.result.data_array:
                    columns = [col.name for col in (result.manifest.schema.columns or [])]
                    return [dict(zip(columns, row)) for row in result.result.data_array]
                return []
            except Exception as exc:
                logger.warning(
                    "sql_workspace_failed",
                    workspace=ws_name,
                    error=str(exc),
                )
                continue
        raise APIError("databricks", "No workspace with a reachable SQL warehouse")

    @staticmethod
    def _to_cluster_info(c) -> ClusterInfo:
        autoscale = c.autoscale is not None

        # Derive spot settings from aws_attributes.availability
        spot_enabled = False
        spot_fallback = False
        if c.aws_attributes and c.aws_attributes.availability:
            avail = str(c.aws_attributes.availability.value)
            if avail == "SPOT_WITH_FALLBACK":
                spot_enabled = True
                spot_fallback = True
            elif avail == "SPOT":
                spot_enabled = True

        # Convert epoch milliseconds to datetime
        started_at = None
        start_ms = getattr(c, "start_time", None)
        if start_ms:
            started_at = datetime.fromtimestamp(start_ms / 1000, tz=UTC)
        last_activity_at = None
        activity_ms = getattr(c, "last_activity_time", None) or getattr(
            c, "last_activity_time_millis", None
        )
        if activity_ms:
            last_activity_at = datetime.fromtimestamp(activity_ms / 1000, tz=UTC)

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
            spot_enabled=spot_enabled,
            spot_fallback=spot_fallback,
            autotermination_minutes=c.autotermination_minutes,
            cluster_source=(str(c.cluster_source.value) if c.cluster_source else None),
            creator=c.creator_user_name,
            tags=dict(c.custom_tags) if c.custom_tags else {},
            started_at=started_at,
            last_activity_at=last_activity_at,
        )
