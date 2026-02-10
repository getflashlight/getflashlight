"""Databricks storage/Delta Lake client."""

from __future__ import annotations

from typing import Any

import structlog
from auralake_shared.core.exceptions import APIError
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractStorageClient

from auralake_backend.providers.databricks.auth import get_workspace_client

logger = structlog.get_logger(__name__)


class DatabricksStorageClient(AbstractStorageClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def list_tables(
        self, catalog: str | None = None, schema: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            tables = []
            if catalog and schema:
                result = self._client.tables.list(catalog_name=catalog, schema_name=schema)
                for t in result:
                    tables.append(
                        {
                            "catalog": t.catalog_name,
                            "schema": t.schema_name,
                            "name": t.name,
                            "full_name": t.full_name,
                            "table_type": str(t.table_type.value) if t.table_type else "UNKNOWN",
                        }
                    )
            return tables
        except Exception as exc:
            raise APIError("databricks", f"Failed to list tables: {exc}") from exc

    def discover_all_tables(self) -> list[dict[str, Any]]:
        """Discover all Delta tables across all catalogs and schemas.

        Uses ``information_schema.tables`` via the SQL warehouse to enumerate
        catalogs → schemas → tables without requiring explicit catalog/schema
        arguments. Falls back to the Unity Catalog REST API if SQL fails.
        """
        try:
            rows = self._execute_sql(
                "SELECT table_catalog, table_schema, table_name, table_type "
                "FROM system.information_schema.tables "
                "WHERE table_type = 'MANAGED' OR table_type = 'EXTERNAL' "
                "ORDER BY table_catalog, table_schema, table_name"
            )
            tables = []
            for row in rows:
                full_name = f"{row['table_catalog']}.{row['table_schema']}.{row['table_name']}"
                tables.append(
                    {
                        "catalog": row["table_catalog"],
                        "schema": row["table_schema"],
                        "name": row["table_name"],
                        "full_name": full_name,
                        "table_type": row.get("table_type", "UNKNOWN"),
                    }
                )
            logger.info("tables_discovered", count=len(tables))
            return tables
        except APIError:
            raise
        except Exception as exc:
            logger.warning("table_discovery_sql_failed", error=str(exc))
            # Fall back to Unity Catalog API: enumerate catalogs → schemas → tables
            return self._discover_via_api()

    def _discover_via_api(self) -> list[dict[str, Any]]:
        """Discover tables using the Unity Catalog REST API."""
        tables: list[dict[str, Any]] = []
        try:
            for catalog in self._client.catalogs.list():
                catalog_name = catalog.name
                if not catalog_name or catalog_name.startswith("__"):
                    continue
                try:
                    for schema in self._client.schemas.list(catalog_name=catalog_name):
                        schema_name = schema.name
                        if not schema_name or schema_name == "information_schema":
                            continue
                        try:
                            batch = self.list_tables(catalog_name, schema_name)
                            tables.extend(batch)
                        except Exception as exc:
                            logger.warning(
                                "schema_table_list_failed",
                                catalog=catalog_name,
                                schema=schema_name,
                                error=str(exc),
                            )
                except Exception as exc:
                    logger.warning(
                        "catalog_schema_list_failed",
                        catalog=catalog_name,
                        error=str(exc),
                    )
            logger.info("tables_discovered_via_api", count=len(tables))
        except Exception as exc:
            raise APIError("databricks", f"Failed to discover tables: {exc}") from exc
        return tables

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

    def get_table_stats(self, table_name: str) -> dict[str, Any]:
        try:
            # Use DESCRIBE DETAIL to get Delta table stats
            for ws_name, ws_config in self._config.workspaces.items():
                if ws_config.sql_warehouse_id:
                    client = get_workspace_client(self._config, ws_name)
                    result = client.statement_execution.execute_statement(
                        warehouse_id=ws_config.sql_warehouse_id,
                        statement=f"DESCRIBE DETAIL {table_name}",
                    )
                    if result.result and result.result.data_array:
                        columns = [col.name for col in (result.manifest.schema.columns or [])]
                        row = result.result.data_array[0]
                        return dict(zip(columns, row))
            return {}
        except Exception as exc:
            raise APIError(
                "databricks", f"Failed to get table stats for {table_name}: {exc}"
            ) from exc

    def optimize_table(self, table_name: str) -> dict[str, Any]:
        try:
            for ws_name, ws_config in self._config.workspaces.items():
                if ws_config.sql_warehouse_id:
                    client = get_workspace_client(self._config, ws_name)
                    client.statement_execution.execute_statement(
                        warehouse_id=ws_config.sql_warehouse_id,
                        statement=f"OPTIMIZE {table_name}",
                    )
                    return {"status": "completed", "table": table_name}
            raise APIError("databricks", "No workspace with sql_warehouse_id configured")
        except APIError:
            raise
        except Exception as exc:
            raise APIError("databricks", f"Failed to optimize {table_name}: {exc}") from exc

    def vacuum_table(self, table_name: str, retention_hours: int = 168) -> dict[str, Any]:
        try:
            for ws_name, ws_config in self._config.workspaces.items():
                if ws_config.sql_warehouse_id:
                    client = get_workspace_client(self._config, ws_name)
                    client.statement_execution.execute_statement(
                        warehouse_id=ws_config.sql_warehouse_id,
                        statement=f"VACUUM {table_name} RETAIN {retention_hours} HOURS",
                    )
                    return {
                        "status": "completed",
                        "table": table_name,
                        "retention_hours": retention_hours,
                    }
            raise APIError("databricks", "No workspace with sql_warehouse_id configured")
        except APIError:
            raise
        except Exception as exc:
            raise APIError("databricks", f"Failed to vacuum {table_name}: {exc}") from exc
