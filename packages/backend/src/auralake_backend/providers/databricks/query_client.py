"""Databricks Query History API client."""

from __future__ import annotations

from typing import Any

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractQueryClient

from auralake_backend.providers.databricks.auth import get_workspace_client


class DatabricksQueryClient(AbstractQueryClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def _fetch_query_history(
        self, filter_by: Any, limit: int
    ) -> list[dict[str, Any]]:
        """Paginate through query_history.list and return flattened results."""
        results: list[dict[str, Any]] = []
        page_token: str | None = None

        while len(results) < limit:
            page_limit = min(limit - len(results), 100)
            resp = self._client.query_history.list(
                filter_by=filter_by,
                max_results=page_limit,
                page_token=page_token,
            )
            for q in resp.res or []:
                results.append(
                    {
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
                    }
                )
            if not resp.has_next_page or not resp.next_page_token:
                break
            page_token = resp.next_page_token

        return results

    def get_query_history(self, hours: int = 24, limit: int = 1000) -> list[dict[str, Any]]:
        """Fetch recent query history from Databricks."""
        try:
            from datetime import datetime, timedelta

            from databricks.sdk.service.sql import QueryFilter, TimeRange

            start_time = datetime.utcnow() - timedelta(hours=hours)
            end_time = datetime.utcnow()

            filter_by = QueryFilter(
                query_start_time_range=TimeRange(
                    start_time_ms=int(start_time.timestamp() * 1000),
                    end_time_ms=int(end_time.timestamp() * 1000),
                ),
            )
            return self._fetch_query_history(filter_by, limit)
        except APIError:
            raise
        except Exception as exc:
            raise APIError("databricks", f"Failed to fetch query history: {exc}") from exc

    def get_query_history_since(self, since_ms: int, limit: int = 1000) -> list[dict[str, Any]]:
        """Fetch queries that ended after ``since_ms`` (epoch milliseconds)."""
        try:
            from datetime import datetime

            from databricks.sdk.service.sql import QueryFilter, TimeRange

            end_time = datetime.utcnow()

            filter_by = QueryFilter(
                query_start_time_range=TimeRange(
                    start_time_ms=since_ms,
                    end_time_ms=int(end_time.timestamp() * 1000),
                ),
            )
            return self._fetch_query_history(filter_by, limit)
        except APIError:
            raise
        except Exception as exc:
            raise APIError(
                "databricks", f"Failed to fetch query history since {since_ms}: {exc}"
            ) from exc

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
