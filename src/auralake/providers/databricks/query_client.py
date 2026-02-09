"""Databricks Query History API client."""
from __future__ import annotations

from typing import Any

from auralake.core.exceptions import APIError
from auralake.models.config import DatabricksConfig
from auralake.providers.base import AbstractQueryClient
from auralake.providers.databricks.auth import get_workspace_client


class DatabricksQueryClient(AbstractQueryClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def get_query_history(self, hours: int = 24, limit: int = 1000) -> list[dict[str, Any]]:
        """Fetch recent query history from Databricks."""
        try:
            from datetime import datetime, timedelta
            from databricks.sdk.service.sql import QueryFilter, TimeRange

            start_time = datetime.utcnow() - timedelta(hours=hours)
            end_time = datetime.utcnow()

            queries = self._client.query_history.list(
                filter_by=QueryFilter(
                    query_start_time_range=TimeRange(
                        start_time_ms=int(start_time.timestamp() * 1000),
                        end_time_ms=int(end_time.timestamp() * 1000),
                    ),
                ),
                max_results=limit,
            )
            results = []
            for q in queries:
                results.append({
                    "query_id": q.query_id,
                    "query_text": q.query_text,
                    "status": str(q.status.value) if q.status else "UNKNOWN",
                    "user_name": q.user_name,
                    "warehouse_id": q.warehouse_id,
                    "duration_ms": q.duration,
                    "rows_produced": q.rows_produced,
                    "executed_as_user_name": q.executed_as_user_name,
                    "query_start_time_ms": q.query_start_time_ms,
                    "query_end_time_ms": q.query_end_time_ms,
                })
            return results
        except Exception as exc:
            raise APIError("databricks", f"Failed to fetch query history: {exc}") from exc

    def execute_query(self, sql: str) -> list[dict[str, Any]]:
        """Execute a SQL query via statement execution API."""
        try:
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
        except APIError:
            raise
        except Exception as exc:
            raise APIError("databricks", f"Failed to execute query: {exc}") from exc

    def explain_query(self, sql: str) -> str:
        """Run EXPLAIN on a query and return the plan text."""
        results = self.execute_query(f"EXPLAIN {sql}")
        if results:
            return "\n".join(str(row.get("plan", "")) for row in results)
        return ""
