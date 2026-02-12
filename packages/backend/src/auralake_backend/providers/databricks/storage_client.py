"""Databricks storage/Delta Lake client."""

from __future__ import annotations

from typing import Any

import structlog
from auralake_shared.core.exceptions import APIError
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractStorageClient

from auralake_backend.providers.databricks.auth import get_warehouse_id, get_workspace_client

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
        """Discover all tables across all catalogs and schemas with bulk metadata.

        Queries every configured workspace via ``information_schema.tables``
        and deduplicates by ``full_name`` so tables visible from multiple
        workspaces are only returned once. Falls back to the Unity Catalog
        REST API if SQL fails on all workspaces.
        """
        try:
            rows = self._execute_sql_all_workspaces(
                "SELECT table_catalog, table_schema, table_name, table_type, "
                "data_source_format, table_owner, last_altered, comment "
                "FROM system.information_schema.tables "
                "WHERE table_type IN ('MANAGED', 'EXTERNAL') "
                "ORDER BY table_catalog, table_schema, table_name"
            )
            seen: set[str] = set()
            tables: list[dict[str, Any]] = []
            for row in rows:
                full_name = f"{row['table_catalog']}.{row['table_schema']}.{row['table_name']}"
                if full_name in seen:
                    continue
                seen.add(full_name)
                tables.append(
                    {
                        "catalog": row["table_catalog"],
                        "schema": row["table_schema"],
                        "name": row["table_name"],
                        "full_name": full_name,
                        "table_type": row.get("table_type", "UNKNOWN"),
                        "data_source_format": row.get("data_source_format"),
                        "owner": row.get("table_owner"),
                        "last_altered": row.get("last_altered"),
                    }
                )
            logger.info("tables_discovered", count=len(tables))
            return tables
        except APIError:
            raise
        except Exception as exc:
            logger.warning("table_discovery_sql_failed", error=str(exc))
            # Fall back to Unity Catalog API: enumerate catalogs -> schemas -> tables
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
                            result = self._client.tables.list(
                                catalog_name=catalog_name, schema_name=schema_name
                            )
                            for t in result:
                                tables.append(
                                    {
                                        "catalog": t.catalog_name,
                                        "schema": t.schema_name,
                                        "name": t.name,
                                        "full_name": t.full_name,
                                        "table_type": (
                                            str(t.table_type.value) if t.table_type else "UNKNOWN"
                                        ),
                                        "data_source_format": (
                                            str(t.data_source_format.value)
                                            if t.data_source_format
                                            else None
                                        ),
                                        "owner": t.owner,
                                    }
                                )
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

    def _execute_sql_all_workspaces(self, sql: str) -> list[dict]:
        """Execute SQL against every workspace and merge results."""
        all_rows: list[dict] = []
        errors: list[str] = []

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
                    rows = [dict(zip(columns, row)) for row in result.result.data_array]
                    all_rows.extend(rows)
                    logger.info(
                        "sql_workspace_results",
                        workspace=ws_name,
                        rows=len(rows),
                    )
            except Exception as exc:
                errors.append(f"{ws_name}: {exc}")
                logger.warning(
                    "sql_workspace_failed",
                    workspace=ws_name,
                    error=str(exc),
                )

        if not all_rows and errors:
            raise APIError("databricks", f"SQL failed on all workspaces: {'; '.join(errors)}")
        return all_rows

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

    def get_table_stats(self, table_name: str) -> dict[str, Any]:
        """Try each workspace until DESCRIBE DETAIL succeeds for the table."""
        errors: list[str] = []
        for ws_name, ws_config in self._config.workspaces.items():
            try:
                client = get_workspace_client(self._config, ws_name)
                wh_id = get_warehouse_id(
                    client, ws_config.sql_warehouse_id, prefer_pro=True
                )
                result = client.statement_execution.execute_statement(
                    warehouse_id=wh_id,
                    statement=f"DESCRIBE DETAIL {table_name}",
                )
                if result.result and result.result.data_array:
                    columns = [col.name for col in (result.manifest.schema.columns or [])]
                    row = result.result.data_array[0]
                    return dict(zip(columns, row))
            except Exception as exc:
                errors.append(f"{ws_name}: {exc}")
                continue
        if errors:
            raise APIError(
                "databricks",
                f"Failed to get table stats for {table_name}: {'; '.join(errors)}",
            )
        return {}

    def get_table_history(self, table_name: str, limit: int = 100) -> list[dict[str, Any]]:
        """Try each workspace until DESCRIBE HISTORY succeeds for the table."""
        errors: list[str] = []
        for ws_name, ws_config in self._config.workspaces.items():
            try:
                client = get_workspace_client(self._config, ws_name)
                wh_id = get_warehouse_id(
                    client, ws_config.sql_warehouse_id, prefer_pro=True
                )
                result = client.statement_execution.execute_statement(
                    warehouse_id=wh_id,
                    statement=f"DESCRIBE HISTORY {table_name} LIMIT {limit}",
                )
                if result.result and result.result.data_array:
                    columns = [col.name for col in (result.manifest.schema.columns or [])]
                    rows = [dict(zip(columns, row)) for row in result.result.data_array]
                    maintenance_ops = {"OPTIMIZE", "VACUUM START", "VACUUM END"}
                    return [r for r in rows if r.get("operation") in maintenance_ops]
                return []
            except Exception as exc:
                errors.append(f"{ws_name}: {exc}")
                continue
        if errors:
            raise APIError(
                "databricks",
                f"Failed to get table history for {table_name}: {'; '.join(errors)}",
            )
        return []

    def optimize_table(self, table_name: str) -> dict[str, Any]:
        try:
            self._execute_sql(f"OPTIMIZE {table_name}")
            return {"status": "completed", "table": table_name}
        except APIError:
            raise
        except Exception as exc:
            raise APIError("databricks", f"Failed to optimize {table_name}: {exc}") from exc

    def vacuum_table(self, table_name: str, retention_hours: int = 168) -> dict[str, Any]:
        try:
            self._execute_sql(f"VACUUM {table_name} RETAIN {retention_hours} HOURS")
            return {
                "status": "completed",
                "table": table_name,
                "retention_hours": retention_hours,
            }
        except APIError:
            raise
        except Exception as exc:
            raise APIError("databricks", f"Failed to vacuum {table_name}: {exc}") from exc
