"""Databricks connector — runs the authoritative System Tables → FOCUS 1.3 query.

Rather than hand-map ``system.billing.usage``, we execute the Databricks-authored
mapping (vendored at ``sql/databricks_focus_1_3.sql``) on a SQL warehouse. Its
output is already FOCUS, so each row goes through the shared ``map_focus_row``.

The one thing FOCUS doesn't carry is compute class (classic vs serverless), which
the TCO double-count guard needs — we derive it from the SKU here.

Requires Unity Catalog access to: system.billing.usage, system.billing.list_prices,
system.access.workspaces_latest, system.compute.clusters, system.compute.warehouses,
system.lakeflow.pipelines.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient

from auralake.core.exceptions import ConnectorError
from auralake.core.logging import get_logger
from auralake.focus.enums import ComputeClass
from auralake.focus.model import FocusRecord
from auralake.ingest.base import Connector, IngestWindow
from auralake.ingest.config import DatabricksConfig, env
from auralake.ingest.connectors._focus_map import map_focus_row

logger = get_logger(__name__)

_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_focus_1_3.sql"
_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"}


def compute_class_for_sku(sku: str | None) -> ComputeClass:
    """Classic vs serverless from the SKU name (drives the TCO double-count guard)."""
    return ComputeClass.SERVERLESS if sku and "SERVERLESS" in sku.upper() else ComputeClass.CLASSIC


class DatabricksConnector(Connector):
    name = "databricks"

    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        token = env(config.token_env)
        if not token:
            raise ConnectorError(self.name, f"Missing token env {config.token_env}")
        self._client = WorkspaceClient(host=config.host, token=token)

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        for row in self._execute(self._render_query(window)):
            record = map_focus_row(row, self.name)
            if record is None:
                continue
            yield record.model_copy(
                update={
                    "x_compute_class": compute_class_for_sku(record.sku_id),
                    "x_focus_version": "1.3",
                }
            )

    def _render_query(self, window: IngestWindow) -> str:
        sql = _QUERY_PATH.read_text()
        # Substitute the account-prices table parameter (a config-controlled identifier).
        sql = sql.replace(":account_prices", f"'{self._config.account_prices_table}'")
        # Bound the scan to the ingest window (final FROM aliases the CTE as `u`).
        sql = sql.rstrip().rstrip(";")
        return f"{sql}\nWHERE u.usage_date BETWEEN '{window.start}' AND '{window.end}'"

    def _warehouse_id(self) -> str:
        if self._config.sql_warehouse_id:
            return self._config.sql_warehouse_id
        for wh in self._client.warehouses.list():
            if wh.id:
                return wh.id
        raise ConnectorError(self.name, "No SQL warehouse available")

    def _execute(self, sql: str) -> list[dict[str, Any]]:
        exec_api = self._client.statement_execution
        try:
            resp = exec_api.execute_statement(
                warehouse_id=self._warehouse_id(), statement=sql, wait_timeout="50s"
            )
            resp = self._await_completion(resp)
            manifest = resp.manifest
            if not (manifest and manifest.schema):
                return []
            cols = [c.name or "" for c in (manifest.schema.columns or [])]

            rows: list[dict[str, Any]] = []
            chunk = resp.result
            while chunk and chunk.data_array:
                rows.extend(dict(zip(cols, r, strict=False)) for r in chunk.data_array)
                if chunk.next_chunk_index is None:
                    break
                chunk = exec_api.get_statement_result_chunk_n(
                    resp.statement_id, chunk.next_chunk_index
                )
            return rows
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"SQL failed: {exc}") from exc

    def _await_completion(self, resp: Any) -> Any:
        """Poll until the statement reaches a terminal state."""
        exec_api = self._client.statement_execution
        for _ in range(120):  # ~2 min ceiling beyond the initial wait_timeout
            state = resp.status.state.value if (resp.status and resp.status.state) else None
            if state in _TERMINAL_STATES:
                if state != "SUCCEEDED":
                    raise ConnectorError(self.name, f"Statement {state}")
                return resp
            time.sleep(1)
            resp = exec_api.get_statement(resp.statement_id)
        raise ConnectorError(self.name, "Statement did not complete in time")
