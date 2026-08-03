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
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import Disposition, Format

from flashlight.core.exceptions import ConnectorError, FocusValidationError
from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.focus import sql_mapping
from flashlight.focus.enums import ComputeClass
from flashlight.ingest.base import Connector, IngestWindow, ProgressCallback
from flashlight.ingest.config import DatabricksConfig, effective_connector_name, env
from flashlight.ingest.connectors._coerce import to_decimal
from flashlight.lake import bronze
from flashlight.lake.driver_health_schema import DriverHealthRecord

logger = get_logger(__name__)

_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_focus_1_3.sql"
_EFFICIENCY_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_efficiency.sql"
_DRIVER_HEALTH_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_driver_health.sql"
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

# Candidate Delta tables for the size/compression inventory (top-N by size, monthly).
# Column names per Databricks' published information_schema.tables docs
# (docs.databricks.com/aws/en/sql/language-manual/information-schema/tables) — NOT YET
# VALIDATED against a live workspace. Re-run `flashlight ingest` and spot-check
# cause_detail.size_bytes/compression_codec on a `table`-entity row before trusting it.
#
# Confirmed against a live workspace 2026-07-04: excludes `__databricks_internal` (DLT's
# own materialization-schema tables live there) and `event_log_%`-named tables (DLT's
# per-pipeline event log, published into the PIPELINE'S OWN target catalog/schema, not
# just __databricks_internal — seen in general_catalog.leadsrx_ml_secure_silver too).
# Both 4xx PERMISSION_DENIED on DESCRIBE DETAIL (only the DLT pipeline owner can read
# them), which is pure noise for a top-N-by-size inventory aimed at real user tables.
#
# No ESCAPE clause: `_` in LIKE already matches a literal underscore as a wildcard, so
# 'event_log_%' matches real event_log_<uuid> names with no escaping needed — tried an
# explicit `ESCAPE '\'` first, but Databricks SQL's string-literal unescaping mangles a
# single backslash inside a quoted literal into a parse error (confirmed live: even
# `SELECT ... ESCAPE '\'` alone fails with PARSE_SYNTAX_ERROR). Simpler pattern, no
# escaping, verified against a live warehouse 2026-07-04.
_TABLE_INVENTORY_CANDIDATES_SQL = """
SELECT table_catalog, table_schema, table_name
FROM system.information_schema.tables
WHERE data_source_format = 'DELTA'
  AND table_catalog != '__databricks_internal'
  AND table_name NOT LIKE 'event_log_%'
ORDER BY last_altered DESC
LIMIT 200
"""
# ponytail: 200 recently-altered tables is the pool DESCRIBE DETAIL runs against, so a
# large metastore can't turn this into thousands of per-table round trips; raise if a
# genuinely huge, rarely-touched table needs to be seen.
_TABLE_INVENTORY_TOP_N = 20
# ponytail: bounded worker pool so DESCRIBE DETAIL calls run concurrently against the
# warehouse instead of one at a time (200 tables serially was ~4 min of pure round-trip
# wait); raise if the warehouse's own concurrency limit comfortably allows more.
_TABLE_INVENTORY_CONCURRENCY = 8


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


def _compute_class_sql() -> str:
    """The vectorized path's ``x_compute_class`` — same rule as
    :func:`compute_class_for_sku`, evaluated over the raw ``nz(SkuId)`` source
    column instead of Python (see ``sql_mapping.mapping_sql``'s
    ``compute_class_sql`` — spliced into the same SELECT list that computes
    ``sku_id``, so it must read the raw column, not that sibling alias).
    """
    return (
        f"CASE WHEN nz(SkuId) IS NOT NULL AND upper(nz(SkuId)) LIKE '%SERVERLESS%' "
        f"THEN '{ComputeClass.SERVERLESS.value}' ELSE '{ComputeClass.CLASSIC.value}' END"
    )


def _csv_source_sql(con: duckdb.DuckDBPyConnection, links: list[str], columns: list[str]) -> str:
    """The ``read_csv(...)`` table expression over presigned chunk links.

    ``all_varchar=true`` deliberately skips DuckDB's type inference: every column
    gets re-parsed from text by ``sql_mapping.mapping_sql`` anyway (the same
    tolerant ``nz()``/``try_cast`` treatment a Parquet or local-CSV FOCUS source
    gets), so there's no Databricks-type -> DuckDB-type mapping to maintain here.

    The Statement Execution API's EXTERNAL_LINKS/CSV disposition doesn't document
    whether a chunk's file starts with its own header line — observed in practice
    to sometimes be true for the first chunk (surfaces downstream as a bogus row
    with e.g. ``BillingCurrency = 'BillingCurrency'``, tripping the single-currency
    assert). ``header=false`` + explicit ``names=`` (from the manifest schema,
    not the file) handles the common headerless case; probing chunk 0's own first
    row and skipping it when it's verbatim the header is what catches the other
    case without guessing which one applies.
    """
    names = "[" + ", ".join(_sql_str(c) for c in columns) + "]"
    first_relation = (
        f"read_csv({_sql_str(links[0])}, header=false, all_varchar=true, names={names})"
    )
    first_row = con.execute(f"SELECT * FROM {first_relation} LIMIT 1").fetchone()
    if first_row is not None and list(first_row) == columns:
        first_relation = (
            f"read_csv({_sql_str(links[0])}, header=false, all_varchar=true, "
            f"names={names}, skip=1)"
        )
    if len(links) == 1:
        return first_relation
    rest_urls = "[" + ", ".join(_sql_str(link) for link in links[1:]) + "]"
    rest_relation = f"read_csv({rest_urls}, header=false, all_varchar=true, names={names})"
    return f"(SELECT * FROM {first_relation} UNION ALL SELECT * FROM {rest_relation})"


def _sql_str(value: str) -> str:
    """Single-quote a string literal for inlining into SQL (escapes quotes)."""
    return "'" + value.replace("'", "''") + "'"


def _opt_float(value: object) -> float | None:
    """Parse an optional numeric (warehouse values arrive as strings/None)."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _opt_int(value: object) -> int | None:
    f = _opt_float(value)
    return None if f is None else int(f)


def _coerce_properties(value: Any) -> dict[str, str]:
    """Tolerant MAP<STRING,STRING> coercion for DESCRIBE DETAIL's `properties` column.

    Not yet confirmed how the SQL Statement Execution API serializes MAP columns for
    this endpoint (dict, list of (k, v) pairs, or a JSON string are all plausible —
    mirrors the same uncertainty ``_focus_map._tags`` handles for FOCUS `Tags`).
    """
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, list):
        try:
            return {str(k): str(v) for k, v in value}
        except (TypeError, ValueError):
            return {}
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    return {}


class DatabricksConnector(Connector):
    name = "databricks"

    def __init__(self, config: DatabricksConfig) -> None:
        self._config = config
        # Instance-level, shadowing the class constant above — see aws_focus.py's
        # AwsFocusConnector.__init__ for why (BRONZE partitioning stays distinct
        # across multiple Databricks-workspace connections).
        self.name = effective_connector_name(config)
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

    def ingest(
        self,
        window: IngestWindow,
        *,
        run_id: str,
        on_progress: ProgressCallback | None = None,
    ) -> int:
        """Vectorized bulk path: the FOCUS query's result is staged by Databricks as
        presigned-URL CSV chunks (``Disposition.EXTERNAL_LINKS``) regardless of size —
        DuckDB reads those links straight off cloud storage and maps them
        (:mod:`flashlight.focus.sql_mapping`), same as ``aws_focus``. No
        per-row download-parse-``FocusRecord`` loop: for ``system.billing.usage`` at
        real-account scale that loop was the actual bottleneck (millions of rows/month
        through ``json.loads`` + a Python dict + Pydantic validation, three passes for
        no reason once the data's already columnar on disk).

        ``fetch_efficiency``/``fetch_driver_health``/table inventory stay on the
        row-based :meth:`_execute` (JSON_ARRAY) below — their result sets are
        pre-aggregated by the vendored SQL (LIMIT/TOP_N), nowhere near this scale, so
        there's nothing to vectorize there.
        """
        account_prices_table = self._resolve_account_prices()
        effective_is_list = account_prices_table == _LIST_PRICES_TABLE
        logger.info(
            "databricks_ingest_start",
            window_start=str(window.start),
            window_end=str(window.end),
            account_prices_table=account_prices_table,
            effective_is_list=effective_is_list,
        )

        links, columns = self._execute_external_links(self._render_vectorized_query(window))
        if not links:
            logger.info("databricks_ingest_empty")
            return 0

        con = duckdb.connect()
        try:
            con.execute("INSTALL httpfs; LOAD httpfs;")
            con.execute("SET http_timeout = 180;")
            sql_mapping.ensure_helpers(con)
            source_sql = _csv_source_sql(con, links, columns)
            present = sql_mapping.present_columns(con, source_sql)
            mapped = sql_mapping.mapping_sql(
                source_sql,
                connector=self.name,
                run_id=run_id,
                focus_version="1.3",
                present=present,
                compute_class_sql=_compute_class_sql(),
                effective_is_list=effective_is_list,
            )
            written = bronze.write_window_sql(
                self.name, window, con, mapped, base_currency=get_settings().base_currency
            )
        except (ConnectorError, FocusValidationError):
            raise
        except Exception as exc:  # noqa: BLE001 - surface as a connector failure
            raise ConnectorError(self.name, f"DuckDB ingest failed: {exc}") from exc
        finally:
            con.close()
        logger.info("databricks_ingest_done", chunks=len(links), rows=written)
        return written

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

    def _render_vectorized_query(self, window: IngestWindow) -> str:
        """``_render_query`` patched for CSV-safe result staging.

        Spark's CSV writer can't serialize a ``MAP`` cell (the vendored query's
        ``Tags`` column, ``u.custom_tags`` — a real ``MAP<STRING, STRING>``, unlike
        every other FOCUS column here which is already scalar) — stringify it to JSON
        ourselves before requesting ``Format.CSV``, the same ``to_json`` treatment
        :func:`flashlight.focus.sql_mapping._stringify` gives a structured column read
        from Parquet. Substituted in the final projection rather than wrapped in an
        outer ``SELECT * REPLACE (...)`` — that star-syntax extension isn't parsed by
        every SQL warehouse version (PARSE_SYNTAX_ERROR on some), while a plain
        column substitution needs no engine feature beyond baseline SQL.
        """
        sql = self._render_query(window)
        patched = sql.replace("u.custom_tags AS Tags,", "to_json(u.custom_tags) AS Tags,", 1)
        if patched == sql:
            raise ConnectorError(
                self.name,
                "vendored FOCUS query no longer has the expected "
                "'u.custom_tags AS Tags,' projection — update the Tags substitution "
                "in _render_vectorized_query to match the new query text.",
            )
        return patched

    def _execute_external_links(self, sql: str) -> tuple[list[str], list[str]]:
        """Run ``sql`` and return (presigned chunk links, column names) — no download,
        no JSON parse, no per-row Python. DuckDB reads the links directly in
        :meth:`ingest`; unlike :meth:`_execute`, this never materializes a single row
        in this process.
        """
        exec_api = self._client.statement_execution
        warehouse_id = self._warehouse_id()
        logger.debug(
            "databricks_statement_submit", warehouse_id=warehouse_id, sql_chars=len(sql)
        )
        try:
            resp = exec_api.execute_statement(
                warehouse_id=warehouse_id,
                statement=sql,
                wait_timeout="50s",
                disposition=Disposition.EXTERNAL_LINKS,
                format=Format.CSV,
            )
            resp = self._await_completion(resp)
            manifest = resp.manifest
            if not (manifest and manifest.schema):
                logger.debug("databricks_statement_done", statement_id=resp.statement_id, rows=0)
                return [], []
            columns = [c.name or "" for c in (manifest.schema.columns or [])]

            # Same "iterate every chunk by index" reasoning as _execute: the
            # continuation index lives on the ExternalLink, not ResultData.
            total_chunks = manifest.total_chunk_count or 1
            links: list[str] = []
            for idx in range(total_chunks):
                data = (
                    resp.result
                    if idx == 0
                    else exec_api.get_statement_result_chunk_n(resp.statement_id, idx)
                )
                chunk_links = [
                    link.external_link
                    for link in ((data.external_links if data else None) or [])
                    if link.external_link
                ]
                if not chunk_links:
                    raise ConnectorError(
                        self.name, f"chunk {idx}/{total_chunks} returned no external link"
                    )
                links.extend(chunk_links)

            logger.info(
                "databricks_statement_done",
                statement_id=resp.statement_id,
                chunks=total_chunks,
                expected_rows=manifest.total_row_count,
            )
            return links, columns
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ConnectorError(self.name, f"SQL failed: {exc}") from exc

    def fetch_efficiency(self, window: IngestWindow) -> Iterator[EfficiencyRecord]:
        """Yield aggregated EfficiencyRecord rows (entity × month) for the window.

        Runs the vendored efficiency aggregation (``sql/databricks_efficiency.sql``) on
        the warehouse — billing.usage ⋈ list_prices for $, node_timeline for utilization,
        job_run_timeline for run/failure counts — and maps each row. Reuses the same
        warehouse + account-prices resolution as the FOCUS pull, so rates match.
        """
        sql = self._render_efficiency_query(window)
        fetched = mapped = 0
        for row in self._execute(sql):
            fetched += 1
            record = self._to_efficiency(row)
            if record is not None:
                mapped += 1
                yield record
        logger.info("databricks_efficiency_done", rows_fetched=fetched, rows_mapped=mapped)
        yield from self._fetch_table_inventory(window)

    def fetch_driver_health(self, window: IngestWindow) -> Iterator[DriverHealthRecord]:
        """Yield aggregated DriverHealthRecord rows (driver × application × user × month).

        Independent of :meth:`fetch_efficiency` — its own vendored query
        (``sql/databricks_driver_health.sql``) against ``system.query.history``, no
        cause_detail/waste semantics. A fleet-health/compliance view, not a cost signal.
        """
        sql = self._render_driver_health_query(window)
        fetched = mapped = 0
        for row in self._execute(sql):
            fetched += 1
            record = self._to_driver_health(row)
            if record is not None:
                mapped += 1
                yield record
        logger.info("databricks_driver_health_done", rows_fetched=fetched, rows_mapped=mapped)

    def _render_driver_health_query(self, window: IngestWindow) -> str:
        sql = _DRIVER_HEALTH_QUERY_PATH.read_text()
        return (
            sql.replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end}'")
        )

    @staticmethod
    def _to_driver_health(row: dict[str, Any]) -> DriverHealthRecord | None:
        """Map one aggregation row → DriverHealthRecord (values arrive as strings/None)."""
        charge_month = row.get("charge_month")
        if not charge_month:
            return None
        return DriverHealthRecord(
            provider_name="Databricks",
            charge_month=date.fromisoformat(str(charge_month)[:10]),
            client_driver=row.get("client_driver") or None,
            client_application=row.get("client_application") or None,
            executed_by=row.get("executed_by") or None,
            query_count=_opt_int(row.get("query_count")) or 0,
            x_source_connector="databricks",
        )

    def _fetch_table_inventory(self, window: IngestWindow) -> Iterator[EfficiencyRecord]:
        """Top-N Delta tables by size, snapshotted for this window's month.

        No dollar figure: Databricks doesn't bill storage per-table (that's the
        underlying cloud storage bill), so ``billed_cost`` stays 0 rather than
        inventing one. This is a size/compression inventory signal for a future
        compression-migration rule — not itself a priced WasteRule yet (see
        ``missing_delta_optimization`` in waste_rules.py, still BLOCKED pending $
        attribution). Best-effort per table: one bad DESCRIBE DETAIL is skipped
        rather than losing the whole inventory (or the compute-efficiency rows
        already yielded above).
        """
        try:
            candidates = self._execute(_TABLE_INVENTORY_CANDIDATES_SQL)
        except ConnectorError as exc:
            logger.warning("databricks_table_inventory_candidates_failed", error=str(exc))
            return

        names = [
            f"`{c.get('table_catalog')}`.`{c.get('table_schema')}`.`{c.get('table_name')}`"
            for c in candidates
        ]
        sized: list[tuple[int, str, dict[str, Any]]] = []
        # DESCRIBE DETAIL calls are independent, stateless statements — run them
        # concurrently against the warehouse instead of one at a time (was ~4 min
        # serial for 200 tables, almost entirely round-trip wait, not compute).
        with ThreadPoolExecutor(max_workers=_TABLE_INVENTORY_CONCURRENCY) as pool:
            for full_name, detail in zip(names, pool.map(self._describe_detail, names)):
                if not detail:
                    continue
                size_bytes = _opt_int(detail.get("sizeInBytes")) or 0
                sized.append((size_bytes, full_name, detail))

        sized.sort(key=lambda item: item[0], reverse=True)
        # DESCRIBE DETAIL only ever reflects CURRENT size — Delta has no historical-size
        # API — so this snapshot is "as of today", never a past month. Stamping it with
        # window.start would mislabel a snapshot taken now as if it were taken back then
        # on a wide backfill window. Real month-over-month history only builds up from
        # repeated `flashlight ingest` runs, one snapshot per run.
        month = date.today().replace(day=1)
        mapped = 0
        for size_bytes, full_name, row in sized[:_TABLE_INVENTORY_TOP_N]:
            properties = _coerce_properties(row.get("properties"))
            cause = {
                "num_files": _opt_int(row.get("numFiles")),
                "compression_codec": properties.get("delta.parquet.compression.codec"),
            }
            mapped += 1
            yield EfficiencyRecord(
                provider_name="Databricks",
                charge_month=month,
                entity_type=EntityType.TABLE,
                entity_id=full_name,
                entity_name=full_name,
                native_quantity=float(size_bytes),
                native_unit="bytes",
                cause_detail={k: v for k, v in cause.items() if v is not None},
                x_source_connector=self.name,
            )
        logger.info(
            "databricks_table_inventory_done",
            candidates=len(candidates),
            sized=len(sized),
            rows_mapped=mapped,
        )

    def _describe_detail(self, full_name: str) -> dict[str, Any] | None:
        try:
            detail = self._execute(f"DESCRIBE DETAIL {full_name}")
        except ConnectorError as exc:
            logger.warning("databricks_describe_detail_failed", table=full_name, error=str(exc))
            return None
        return detail[0] if detail else None

    def _render_efficiency_query(self, window: IngestWindow) -> str:
        sql = _EFFICIENCY_QUERY_PATH.read_text()
        sql = sql.replace(":account_prices", f"'{self._resolve_account_prices()}'")
        return (
            sql.replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end}'")
        )

    @staticmethod
    def _to_efficiency(row: dict[str, Any]) -> EfficiencyRecord | None:
        """Map one aggregation row → EfficiencyRecord (values arrive as strings/None)."""
        entity_id = row.get("entity_id")
        charge_month = row.get("charge_month")
        if not entity_id or not charge_month:
            return None
        try:
            entity_type = EntityType(str(row.get("entity_type")))
        except ValueError:
            return None  # unknown compute class — skip rather than invent
        cause = {
            "run_count": _opt_int(row.get("run_count")),
            "pct_runs_underutilized": _opt_float(row.get("pct_runs_underutilized")),
            "failed_cost": _opt_float(row.get("failed_cost")),
            "photon": str(row.get("photon")).lower() in ("true", "1"),
            # Cluster-config signals (ALL_PURPOSE only — see databricks_efficiency.sql).
            "min_autoscale_workers": _opt_int(row.get("min_autoscale_workers")),
            "max_autoscale_workers": _opt_int(row.get("max_autoscale_workers")),
            "auto_termination_minutes": _opt_int(row.get("auto_termination_minutes")),
            "worker_node_type": row.get("worker_node_type") or None,
            "core_count": _opt_float(row.get("core_count")),
            "availability": row.get("availability") or None,
            # Peak alongside the avg-based utilization_pct — surfaces skew (one hot
            # executor hiding in a healthy-looking average) as a visibility signal.
            "max_cpu_pct": _opt_float(row.get("max_cpu_pct")),
            "max_mem_pct": _opt_float(row.get("max_mem_pct")),
            # The migratable job-shaped slice of an interactive cluster's spend, and who
            # triggered its largest such job — see 'placement' in waste_rules.py.
            "job_shaped_cost": _opt_float(row.get("job_shaped_cost")),
            "top_job_name": row.get("top_job_name") or None,
            "top_job_owner": row.get("top_job_owner") or None,
            # SQL warehouse query-pattern health (sql_warehouse only) — see
            # 'sql_warehouse_low_cache_reuse' in waste_rules.py.
            "cache_hit_pct": _opt_float(row.get("cache_hit_pct")),
            "query_count": _opt_int(row.get("query_count")),
            # sql_warehouse/sql_warehouse_user: real CLASSIC/PRO/SERVERLESS fact, and
            # (sql_warehouse_user only) per-user cadence + allocation share — see
            # sql_warehouse_serverless_pricing_gap / sql_warehouse_high_frequency_workload /
            # sql_warehouse_user_concentration in waste_rules.py.
            "warehouse_type": row.get("warehouse_type") or None,
            "avg_interval_minutes": _opt_float(row.get("avg_interval_minutes")),
            "duration_share_pct": _opt_float(row.get("duration_share_pct")),
            # Real re-priced-at-the-jobs-compute-SKU dollar figure (interactive/notebook →
            # jobs-compute rate), from the SAME list_prices table already joined for
            # billed_cost — not a flat percentage that goes stale when Databricks reprices
            # a SKU. See placement/notebook_could_move_to_jobs in waste_rules.py. (Photon
            # has no SKU-price counterpart to re-price against — see databricks_efficiency.sql's
            # header comment — so photon_no_gain/photon_on_interactive_cluster price off a
            # flat DBU-consumption multiplier instead.)
            "jobs_priced_cost": _opt_float(row.get("jobs_priced_cost")),
            # sql_warehouse only — disk spill + shuffle from system.query.history. See
            # 'sql_warehouse_disk_spill' in waste_rules.py; shuffle_bytes is visibility-only
            # (no rule reads it — shuffle alone isn't a waste signal).
            "spill_query_count": _opt_int(row.get("spill_query_count")),
            "spilled_bytes": _opt_float(row.get("spilled_bytes")),
            "shuffle_bytes": _opt_float(row.get("shuffle_bytes")),
            # job/interactive only — spill/shuffle proxy signals (no direct measurement
            # exists for these compute classes). See 'possible_memory_pressure'/
            # 'possible_heavy_shuffle' in waste_rules.py.
            "pct_time_high_cpu_wait": _opt_float(row.get("pct_time_high_cpu_wait")),
            "pct_time_high_mem_swap": _opt_float(row.get("pct_time_high_mem_swap")),
            "min_local_disk_free_bytes": _opt_float(row.get("min_local_disk_free_bytes")),
            "network_bytes": _opt_float(row.get("network_bytes")),
            # job only — materiality gate for the proxies above, not a signal itself.
            "avg_run_seconds": _opt_float(row.get("avg_run_seconds")),
            # Policy-compliance signals (interactive/job clusters via cluster_meta,
            # sql_warehouse/sql_warehouse_user via warehouse_meta; NULL for notebook — no
            # cluster/warehouse identity). policy_id is cluster-only (no warehouse
            # counterpart). tag_count is a resource-level tag count (system.compute.
            # clusters/warehouses.tags), distinct from the per-usage-row `project` tag
            # above — see policy_rules.py's cluster_tagging/warehouse_tagging/
            # cluster_policy_assigned categories.
            "policy_id": row.get("policy_id") or None,
            "tag_count": _opt_int(row.get("tag_count")),
        }
        return EfficiencyRecord(
            provider_name="Databricks",
            charge_month=date.fromisoformat(str(charge_month)[:10]),
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_name=row.get("entity_name"),
            owner_user=row.get("owner_user"),
            owner_project=row.get("owner_project"),
            billed_cost=to_decimal(row.get("billed_cost")),
            native_quantity=_opt_float(row.get("native_quantity")),
            native_unit="DBU",
            utilization_pct=_opt_float(row.get("utilization_pct")),
            activity_count=_opt_int(row.get("activity_count")),
            cause_detail={k: v for k, v in cause.items() if v is not None},
            x_source_connector="databricks",
        )

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

            # Iterate EVERY chunk by index (0..total-1). The continuation index for
            # EXTERNAL_LINKS lives on the ExternalLink, not ResultData, so following
            # ResultData.next_chunk_index silently stops after chunk 0 — driving by
            # total_chunk_count is unambiguous and can't truncate.
            total_chunks = manifest.total_chunk_count or 1
            rows: list[dict[str, Any]] = []
            for idx in range(total_chunks):
                data = (
                    resp.result
                    if idx == 0
                    else exec_api.get_statement_result_chunk_n(resp.statement_id, idx)
                )
                for link in (data.external_links if data else None) or []:
                    rows.extend(
                        dict(zip(cols, r, strict=False))
                        for r in self._download_chunk(link.external_link)
                    )

            # Self-check: the manifest declares the row total; a mismatch means we
            # dropped (or double-read) a chunk — surface it loudly rather than ingest
            # a partial month silently.
            expected = manifest.total_row_count
            if expected is not None and len(rows) != expected:
                logger.error(
                    "databricks_row_count_mismatch",
                    statement_id=resp.statement_id,
                    got=len(rows),
                    expected=expected,
                    chunks=total_chunks,
                    manifest_truncated=manifest.truncated,
                )
                raise ConnectorError(
                    self.name,
                    f"read {len(rows)} rows but manifest declares {expected} "
                    f"({total_chunks} chunks) — refusing partial result",
                )
            logger.info(
                "databricks_statement_done",
                statement_id=resp.statement_id,
                rows=len(rows),
                chunks=total_chunks,
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
