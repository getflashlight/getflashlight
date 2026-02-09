"""Databricks storage/Delta Lake client."""
from __future__ import annotations

from typing import Any

from auralake.core.exceptions import APIError
from auralake.models.config import DatabricksConfig
from auralake.providers.base import AbstractStorageClient
from auralake.providers.databricks.auth import get_workspace_client


class DatabricksStorageClient(AbstractStorageClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def list_tables(self, catalog: str | None = None, schema: str | None = None) -> list[dict[str, Any]]:
        try:
            tables = []
            if catalog and schema:
                result = self._client.tables.list(catalog_name=catalog, schema_name=schema)
                for t in result:
                    tables.append({
                        "catalog": t.catalog_name,
                        "schema": t.schema_name,
                        "name": t.name,
                        "full_name": t.full_name,
                        "table_type": str(t.table_type.value) if t.table_type else "UNKNOWN",
                    })
            return tables
        except Exception as exc:
            raise APIError("databricks", f"Failed to list tables: {exc}") from exc

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
            raise APIError("databricks", f"Failed to get table stats for {table_name}: {exc}") from exc

    def optimize_table(self, table_name: str) -> dict[str, Any]:
        try:
            for ws_name, ws_config in self._config.workspaces.items():
                if ws_config.sql_warehouse_id:
                    client = get_workspace_client(self._config, ws_name)
                    result = client.statement_execution.execute_statement(
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
                    result = client.statement_execution.execute_statement(
                        warehouse_id=ws_config.sql_warehouse_id,
                        statement=f"VACUUM {table_name} RETAIN {retention_hours} HOURS",
                    )
                    return {"status": "completed", "table": table_name, "retention_hours": retention_hours}
            raise APIError("databricks", "No workspace with sql_warehouse_id configured")
        except APIError:
            raise
        except Exception as exc:
            raise APIError("databricks", f"Failed to vacuum {table_name}: {exc}") from exc
