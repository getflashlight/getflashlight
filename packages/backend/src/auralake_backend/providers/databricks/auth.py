"""Authentication helpers for Databricks SDK and AWS (boto3)."""

from __future__ import annotations

import threading
from typing import Any

import structlog
from auralake_shared.core.exceptions import AuthenticationError
from auralake_shared.models.config import (
    DatabricksAWSConfig,
    DatabricksConfig,
    DatabricksWorkspaceConfig,
)
from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config

logger = structlog.get_logger(__name__)


def get_workspace_client(
    config: DatabricksConfig, workspace_name: str | None = None
) -> WorkspaceClient:
    """Get a Databricks WorkspaceClient for the given workspace."""
    ws_config = _resolve_workspace(config, workspace_name)
    try:
        cfg_kwargs: dict[str, Any] = {
            "host": ws_config.host,
            "http_timeout_seconds": 30,
            "retry_timeout_seconds": 30,
        }
        if ws_config.token:
            cfg_kwargs["token"] = ws_config.token
        if ws_config.client_id:
            cfg_kwargs["client_id"] = ws_config.client_id
        if ws_config.client_secret:
            cfg_kwargs["client_secret"] = ws_config.client_secret
        return WorkspaceClient(config=Config(**cfg_kwargs))
    except Exception as exc:
        raise AuthenticationError("databricks", f"Failed to authenticate: {exc}") from exc


def _resolve_workspace(config: DatabricksConfig, name: str | None) -> DatabricksWorkspaceConfig:
    if name and name in config.workspaces:
        return config.workspaces[name]
    for ws_name, ws_config in config.workspaces.items():
        if ws_config.is_default:
            return ws_config
    if config.workspaces:
        return next(iter(config.workspaces.values()))
    raise AuthenticationError("databricks", "No workspaces configured")


_SIZE_ORDER = {
    "2X-Small": 0,
    "X-Small": 1,
    "Small": 2,
    "Medium": 3,
    "Large": 4,
    "X-Large": 5,
    "2X-Large": 6,
    "3X-Large": 7,
    "4X-Large": 8,
}


class WarehouseResolver:
    """Caches warehouse discovery per workspace. Thread-safe."""

    def __init__(self, client: WorkspaceClient) -> None:
        self._client = client
        self._lock = threading.Lock()
        self._warehouses: list[dict[str, Any]] | None = None
        self._resolved: dict[tuple[str | None, bool], str] = {}

    def _discover(self) -> list[dict[str, Any]]:
        with self._lock:
            if self._warehouses is not None:
                return self._warehouses
            self._warehouses = []
            for wh in self._client.warehouses.list():
                self._warehouses.append(
                    {
                        "id": wh.id,
                        "name": wh.name,
                        "state": wh.state.value if wh.state else None,
                        "type": wh.warehouse_type.value if wh.warehouse_type else None,
                        "size": wh.cluster_size if wh.cluster_size else None,
                    }
                )
            return self._warehouses

    def get_warehouse_id(
        self, configured_id: str | None = None, *, prefer_pro: bool = False
    ) -> str:
        """Select a SQL warehouse ID.

        When *prefer_pro* is ``False`` (default, existing behaviour):
            configured -> RUNNING PRO/SERVERLESS -> RUNNING any -> STOPPED -> any

        When *prefer_pro* is ``True`` (for bulk DESCRIBE queries):
            configured -> RUNNING PRO (smallest) -> STOPPED PRO (smallest)
            -> RUNNING SERVERLESS -> STOPPED SERVERLESS -> any fallback
        """
        if configured_id:
            return configured_id

        cache_key = (configured_id, prefer_pro)
        cached = self._resolved.get(cache_key)
        if cached is not None:
            return cached

        warehouses = self._discover()

        if prefer_pro:
            result = self._select_prefer_pro(warehouses)
        else:
            result = self._select_default(warehouses)

        if result is None:
            raise AuthenticationError("databricks", "No SQL warehouse found in workspace")

        self._resolved[cache_key] = result
        return result

    def _select_default(self, warehouses: list[dict[str, Any]]) -> str | None:
        """Original selection: RUNNING PRO/SERVERLESS -> RUNNING any -> STOPPED -> any."""
        for wh in warehouses:
            if wh["state"] == "RUNNING" and wh["type"] in ("PRO", "SERVERLESS"):
                logger.info(
                    "warehouse_selected",
                    warehouse_id=wh["id"],
                    strategy="running_pro_serverless",
                )
                return wh["id"]

        for wh in warehouses:
            if wh["state"] == "RUNNING" and wh["id"]:
                logger.info(
                    "warehouse_selected", warehouse_id=wh["id"], strategy="running_any"
                )
                return wh["id"]

        for wh in warehouses:
            if wh["state"] == "STOPPED" and wh["id"]:
                logger.info("warehouse_selected", warehouse_id=wh["id"], strategy="stopped")
                return wh["id"]

        for wh in warehouses:
            if wh["id"]:
                logger.info("warehouse_selected", warehouse_id=wh["id"], strategy="fallback")
                return wh["id"]

        return None

    def _select_prefer_pro(self, warehouses: list[dict[str, Any]]) -> str | None:
        """PRO-first selection for bulk DESCRIBE queries (cheapest option)."""

        def _size_key(wh: dict[str, Any]) -> int:
            return _SIZE_ORDER.get(wh.get("size") or "", 99)

        # 1. RUNNING PRO — smallest first
        running_pro = sorted(
            [w for w in warehouses if w["state"] == "RUNNING" and w["type"] == "PRO"],
            key=_size_key,
        )
        if running_pro:
            wh = running_pro[0]
            logger.info(
                "warehouse_selected",
                warehouse_id=wh["id"],
                name=wh["name"],
                size=wh["size"],
                strategy="running_pro",
            )
            return wh["id"]

        # 2. STOPPED PRO — smallest first (auto-starts on SQL)
        stopped_pro = sorted(
            [w for w in warehouses if w["state"] == "STOPPED" and w["type"] == "PRO"],
            key=_size_key,
        )
        if stopped_pro:
            wh = stopped_pro[0]
            logger.info(
                "warehouse_selected",
                warehouse_id=wh["id"],
                name=wh["name"],
                size=wh["size"],
                strategy="stopped_pro",
            )
            return wh["id"]

        # 3. RUNNING SERVERLESS — only if no PRO exists at all
        for wh in warehouses:
            if wh["state"] == "RUNNING" and wh["type"] == "SERVERLESS":
                logger.info(
                    "warehouse_selected",
                    warehouse_id=wh["id"],
                    strategy="running_serverless_no_pro",
                )
                return wh["id"]

        # 4. STOPPED SERVERLESS — last resort
        for wh in warehouses:
            if wh["state"] == "STOPPED" and wh["type"] == "SERVERLESS":
                logger.info(
                    "warehouse_selected",
                    warehouse_id=wh["id"],
                    strategy="stopped_serverless_no_pro",
                )
                return wh["id"]

        # 5. Any fallback
        for wh in warehouses:
            if wh["id"]:
                logger.info(
                    "warehouse_selected", warehouse_id=wh["id"], strategy="fallback"
                )
                return wh["id"]

        return None


_resolver_cache: dict[str, WarehouseResolver] = {}
_resolver_lock = threading.Lock()


def get_warehouse_id(
    client: WorkspaceClient,
    configured_id: str | None = None,
    *,
    prefer_pro: bool = False,
) -> str:
    """Return a SQL warehouse ID with caching. Same signature — backward compatible."""
    host = client.config.host or ""
    with _resolver_lock:
        resolver = _resolver_cache.get(host)
        if resolver is None:
            resolver = WarehouseResolver(client)
            _resolver_cache[host] = resolver
    return resolver.get_warehouse_id(configured_id, prefer_pro=prefer_pro)


def clear_warehouse_cache() -> None:
    """Clear the resolver cache. For test isolation."""
    with _resolver_lock:
        _resolver_cache.clear()


def get_boto3_session(
    region: str | None = None,
    aws_config: DatabricksAWSConfig | None = None,
) -> object:
    """Get a boto3 session for AWS API calls."""
    import boto3  # type: ignore[import-untyped]

    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    if aws_config:
        if not region:
            kwargs["region_name"] = aws_config.region
        if aws_config.access_key_id:
            kwargs["aws_access_key_id"] = aws_config.access_key_id
        if aws_config.secret_access_key:
            kwargs["aws_secret_access_key"] = aws_config.secret_access_key
        if aws_config.session_token:
            kwargs["aws_session_token"] = aws_config.session_token

    return boto3.Session(**kwargs)
