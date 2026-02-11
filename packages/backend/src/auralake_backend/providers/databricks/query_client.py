"""Databricks Query History API client."""

from __future__ import annotations

from typing import Any

from auralake_shared.core.exceptions import APIError
from auralake_shared.models.config import DatabricksConfig
from auralake_shared.providers.base import AbstractQueryClient

from auralake_backend.providers.databricks.auth import get_warehouse_id, get_workspace_client


class DatabricksQueryClient(AbstractQueryClient):
    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        self._client = get_workspace_client(config)

    def _fetch_query_history(self, filter_by: Any, limit: int) -> list[dict[str, Any]]:
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

    def get_query_history(self, hours: int = 24, limit: int = 500_000) -> list[dict[str, Any]]:
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

    def get_query_history_since(self, since_ms: int, limit: int = 500_000) -> list[dict[str, Any]]:
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

    def get_query_history_sql(self, since_date: str) -> list[dict[str, Any]]:
        """Fetch query history from system.query.history via SQL.

        Much faster than the REST API — single SQL call vs thousands of paginated requests.
        """
        sql = f"""
            SELECT
                statement_id AS query_id,
                statement_text AS query_text,
                execution_status AS status,
                executed_by AS user_name,
                compute.warehouse_id AS warehouse_id,
                total_duration_ms AS duration_ms,
                produced_rows AS rows_produced,
                UNIX_TIMESTAMP(start_time) * 1000 AS query_start_time_ms,
                UNIX_TIMESTAMP(end_time) * 1000 AS query_end_time_ms,
                execution_duration_ms,
                read_rows,
                read_bytes,
                shuffle_read_bytes,
                spilled_local_bytes,
                statement_type
            FROM system.query.history
            WHERE start_time >= '{since_date}'
            ORDER BY start_time
        """
        return self.execute_query(sql)

    def execute_query(self, sql: str) -> list[dict[str, Any]]:
        """Execute a SQL query via statement execution API."""
        try:
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
                except Exception:
                    continue
            raise APIError("databricks", "No workspace with a reachable SQL warehouse")
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
