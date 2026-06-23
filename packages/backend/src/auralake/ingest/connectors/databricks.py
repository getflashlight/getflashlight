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

import json
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format

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

# SQL-warehouse cluster sizes, smallest → largest. Auto-pick prefers the smallest
# (cheapest) warehouse; an unknown/blank size sorts last so it's only a fallback.
_WAREHOUSE_SIZE_ORDER = (
    "2X-Small",
    "X-Small",
    "Small",
    "Medium",
    "Large",
    "X-Large",
    "2X-Large",
    "3X-Large",
    "4X-Large",
)


def _warehouse_size_rank(size: str | None) -> int:
    """Rank a cluster size for cheapest-first ordering; unknowns sort last."""
    if size in _WAREHOUSE_SIZE_ORDER:
        return _WAREHOUSE_SIZE_ORDER.index(size)
    return len(_WAREHOUSE_SIZE_ORDER)


def _warehouse_sort_key(wh: Any) -> tuple[int, int, str]:
    """Cheapest-first key: smallest size, then serverless on a tie, then name.

    Serverless wins ties because it has no idle infra cost; the name is a final
    tiebreak so the pick is deterministic across runs.
    """
    return (
        _warehouse_size_rank(wh.cluster_size),
        0 if wh.enable_serverless_compute else 1,
        wh.name or "",
    )


# Public list/rack rates — always present. The connector falls back to this for the
# account-prices join when no negotiated table is available (effective cost = list).
_LIST_PRICES_TABLE = "system.billing.list_prices"
# Negotiated account rates — an AWS/GCP-only preview, absent in most accounts.
_ACCOUNT_PRICES_TABLE = "system.billing.account_prices"
# Existence probe for the account-prices table (system tables expose it here).
_ACCOUNT_PRICES_PROBE = (
    "SELECT 1 FROM system.information_schema.tables "
    "WHERE table_catalog = 'system' AND table_schema = 'billing' "
    "AND table_name = 'account_prices' LIMIT 1"
)


def _statement_error(resp: Any) -> tuple[str, str]:
    """Pull (error_code, message) out of a failed statement response, defensively.

    The SDK nests the real cause at ``status.error.{error_code,message}``; any of
    those may be absent, so fall back to ``UNKNOWN``/empty rather than raising while
    building the error we're about to report.
    """
    err = getattr(getattr(resp, "status", None), "error", None)
    if err is None:
        return "UNKNOWN", ""
    code = getattr(err, "error_code", None)
    code_str = str(getattr(code, "value", code)) if code else "UNKNOWN"
    return code_str, getattr(err, "message", "") or ""


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
        self._account_prices_table: str | None = None  # resolved lazily on first fetch
        self._resolved_warehouse_id: str | None = None  # cached after first lookup
        logger.info(
            "databricks_connector_init",
            host=config.host,
            warehouse_id=config.sql_warehouse_id or "auto",
        )

    def fetch(self, window: IngestWindow) -> Iterator[FocusRecord]:
        account_prices_table = self._resolve_account_prices()
        effective_is_list = account_prices_table == _LIST_PRICES_TABLE
        logger.info(
            "databricks_fetch_start",
            window_start=str(window.start),
            window_end=str(window.end),
            account_prices_table=account_prices_table,
            effective_is_list=effective_is_list,
        )

        fetched = mapped = skipped = 0
        for row in self._execute(self._render_query(window)):
            fetched += 1
            record = map_focus_row(row, self.name)
            if record is None:
                skipped += 1
                continue
            mapped += 1
            yield record.model_copy(
                update={
                    "x_compute_class": compute_class_for_sku(record.sku_id),
                    "x_focus_version": "1.3",
                    "x_effective_is_list": effective_is_list,
                    # Passthrough identity from the vendored query (see _focus_map drops
                    # unknown columns, so read them off the raw row here).
                    "x_record_id": row.get("x_RecordId"),
                    "x_record_type": row.get("x_RecordType"),
                }
            )

        logger.info(
            "databricks_fetch_done",
            rows_fetched=fetched,
            rows_mapped=mapped,
            rows_skipped=skipped,
        )

    def _resolve_account_prices(self) -> str:
        """Pick the account-prices table: negotiated rates if available, else list.

        The negotiated ``account_prices`` table is an AWS/GCP-only preview that most
        accounts don't have. We probe for it once; if it's absent (or the probe
        fails) we fall back to list rates and warn — EffectiveCost then equals
        ListCost and discounts are NOT reflected, which downstream surfaces via the
        ``x_effective_is_list`` flag rather than hiding.
        """
        if self._account_prices_table is not None:
            return self._account_prices_table

        logger.debug("databricks_account_prices_probe", table=_ACCOUNT_PRICES_TABLE)
        try:
            present = bool(self._execute(_ACCOUNT_PRICES_PROBE))
        except ConnectorError as exc:
            # The probe shares the auth/warehouse path with the real query, so a
            # failure here usually means the connection is broken (bad token, no
            # warehouse) — not just a missing table. Warn loudly; the main query
            # will surface the underlying error next.
            logger.warning(
                "databricks_account_prices_probe_failed",
                error=str(exc),
                fallback=_LIST_PRICES_TABLE,
            )
            present = False

        if present:
            logger.info("databricks_account_prices_found", table=_ACCOUNT_PRICES_TABLE)
            self._account_prices_table = _ACCOUNT_PRICES_TABLE
        else:
            logger.warning(
                "databricks_account_prices_unavailable",
                table=_LIST_PRICES_TABLE,
                detail="no negotiated account-prices table; EffectiveCost = ListCost "
                "(discounts not reflected)",
            )
            self._account_prices_table = _LIST_PRICES_TABLE
        return self._account_prices_table

    def _render_query(self, window: IngestWindow) -> str:
        sql = _QUERY_PATH.read_text()
        # Substitute the resolved account-prices table identifier into the join.
        sql = sql.replace(":account_prices", f"'{self._resolve_account_prices()}'")
        # Bound the scan to the ingest window (final FROM aliases the CTE as `u`).
        sql = sql.rstrip().rstrip(";")
        return f"{sql}\nWHERE u.usage_date BETWEEN '{window.start}' AND '{window.end}'"

    def _warehouse_id(self) -> str:
        if self._resolved_warehouse_id is not None:
            return self._resolved_warehouse_id

        if self._config.sql_warehouse_id:
            self._resolved_warehouse_id = self._config.sql_warehouse_id
            logger.debug(
                "databricks_warehouse_configured", warehouse_id=self._config.sql_warehouse_id
            )
            return self._resolved_warehouse_id

        warehouses = [wh for wh in self._client.warehouses.list() if wh.id]
        if not warehouses:
            raise ConnectorError(self.name, "No SQL warehouse available")

        chosen = min(warehouses, key=_warehouse_sort_key)
        warehouse_id = chosen.id
        assert warehouse_id is not None  # filtered above; for the type checker
        self._resolved_warehouse_id = warehouse_id
        logger.info(
            "databricks_warehouse_autoselected",
            warehouse_id=warehouse_id,
            warehouse_name=chosen.name,
            cluster_size=chosen.cluster_size,
            serverless=bool(chosen.enable_serverless_compute),
        )
        return warehouse_id

    def _execute(self, sql: str) -> list[dict[str, Any]]:
        exec_api = self._client.statement_execution
        warehouse_id = self._warehouse_id()
        logger.debug("databricks_statement_submit", warehouse_id=warehouse_id, sql_chars=len(sql))
        try:
            resp = exec_api.execute_statement(
                warehouse_id=warehouse_id,
                statement=sql,
                wait_timeout="50s",
                # Account-wide usage easily exceeds the 25 MiB INLINE cap, so stage
                # results to presigned external links and download the chunks ourselves.
                disposition=Disposition.EXTERNAL_LINKS,
                format=Format.JSON_ARRAY,
            )
            resp = self._await_completion(resp)
            manifest = resp.manifest
            if not (manifest and manifest.schema):
                logger.debug("databricks_statement_done", statement_id=resp.statement_id, rows=0)
                return []
            cols = [c.name or "" for c in (manifest.schema.columns or [])]

            rows: list[dict[str, Any]] = []
            chunk = resp.result
            while chunk:
                for link in chunk.external_links or []:
                    rows.extend(
                        dict(zip(cols, r, strict=False))
                        for r in self._download_chunk(link.external_link)
                    )
                if chunk.next_chunk_index is None:
                    break
                chunk = exec_api.get_statement_result_chunk_n(
                    resp.statement_id, chunk.next_chunk_index
                )
            logger.debug(
                "databricks_statement_done", statement_id=resp.statement_id, rows=len(rows)
            )
            return rows
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"SQL failed: {exc}") from exc

    @staticmethod
    def _download_chunk(url: str | None) -> list[list[Any]]:
        """Fetch one EXTERNAL_LINKS result chunk and parse it.

        The link is a presigned cloud-storage URL — fetch it WITHOUT auth headers
        (a forwarded token can break the signature). JSON_ARRAY format returns a
        JSON array of row-arrays.
        """
        if not url:
            return []
        with urllib.request.urlopen(url) as fh:  # noqa: S310 (presigned https URL)
            payload = fh.read()
        return json.loads(payload) if payload else []

    def _await_completion(self, resp: Any) -> Any:
        """Poll until the statement reaches a terminal state."""
        exec_api = self._client.statement_execution
        for _ in range(120):  # ~2 min ceiling beyond the initial wait_timeout
            state = resp.status.state.value if (resp.status and resp.status.state) else None
            if state in _TERMINAL_STATES:
                if state != "SUCCEEDED":
                    # Surface Databricks' own error (message + code) — without it,
                    # "Statement FAILED" gives no clue (perms, missing table, syntax).
                    code, message = _statement_error(resp)
                    logger.error(
                        "databricks_statement_failed",
                        statement_id=resp.statement_id,
                        state=state,
                        error_code=code,
                        error=message,
                    )
                    detail = f": {message}" if message else ""
                    raise ConnectorError(self.name, f"Statement {state} [{code}]{detail}")
                return resp
            time.sleep(1)
            resp = exec_api.get_statement(resp.statement_id)
        raise ConnectorError(self.name, "Statement did not complete in time")
