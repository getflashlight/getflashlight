"""Databricks connector — runs the authoritative System Tables → FOCUS 1.3 query.

Rather than hand-map ``system.billing.usage``, we execute the Databricks-authored
mapping (vendored at ``sql/databricks_focus_1_3.sql``) on a SQL warehouse. Its
output is already FOCUS, so each row goes through the shared ``map_focus_row``.

The one thing FOCUS doesn't carry is compute class (classic vs serverless) — we
derive it from the SKU here and stamp it as ``x_compute_class``.

Requires Unity Catalog access to: system.billing.usage, system.billing.list_prices,
system.access.workspaces_latest, system.compute.clusters, system.compute.warehouses,
system.lakeflow.pipelines.

``fetch_storage_locations`` additionally reads Unity Catalog *metadata* over REST (no
warehouse): the metastore summary, the catalog list, and the external-location list. Those
APIs are privilege-filtered rather than all-or-nothing — a non-admin token sees only the
objects it owns or holds a privilege on — so an under-privileged token produces a
**partially complete bucket map, not an error**. That shows up as reduced coverage in the
Backing storage tab, never as a failure, which is why it must be verified against the
account rather than assumed: compare `storage.storage_location` against the metastore's
real external locations. Metastore-admin (or an account admin) sees all of them.

``fetch_compute_instances`` runs its own small vendored query
(``sql/databricks_compute_instances.sql``) against ``system.compute.node_timeline`` on a
SQL warehouse — the cluster/instance-id map that lets the AWS EC2 bill be labelled with
the Databricks cluster behind it. Classic compute only (see that module for the scope
caveat).
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Literal

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
from flashlight.lake.ai_usage_schema import AiUsageRecord
from flashlight.lake.compute_instance_schema import ComputeInstanceRecord
from flashlight.lake.driver_health_schema import DriverHealthRecord
from flashlight.lake.storage_location_schema import StorageLocationRecord

logger = get_logger(__name__)

_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_focus_1_3.sql"
_EFFICIENCY_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_efficiency.sql"
_DRIVER_HEALTH_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_driver_health.sql"
_AI_USAGE_QUERY_PATH = Path(__file__).parent / "sql" / "databricks_ai_usage.sql"
_COMPUTE_INSTANCES_QUERY_PATH = (
    Path(__file__).parent / "sql" / "databricks_compute_instances.sql"
)
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

# ── system.serving (Public Preview) ─────────────────────────────────────────────
# The AI token plane's source. `endpoint_usage` carries per-request token counts and the
# requesting identity; `served_entities` carries the model identity and serving config. Both
# may be absent — the schema is a Public Preview that an account has to have enabled — so the
# connector probes and degrades in three rungs instead of failing (see
# _resolve_serving_tables). Column names were validated against a live warehouse 2026-08-05;
# see the header of sql/databricks_ai_usage.sql for the docs-vs-live deltas.
_SERVING_USAGE_TABLE = "system.serving.endpoint_usage"
_SERVED_ENTITIES_TABLE = "system.serving.served_entities"


def _serving_probe(table_name: str) -> str:
    """Existence probe for a ``system.serving`` table."""
    return (
        "SELECT 1 FROM system.information_schema.tables "
        "WHERE table_catalog = 'system' AND table_schema = 'serving' "
        f"AND table_name = '{table_name}' LIMIT 1"
    )


#: What :meth:`DatabricksConnector._resolve_serving_tables` found.
#: ``full`` — both tables: tokens, model identity and serving mode all land.
#: ``usage_only`` — ``endpoint_usage`` but no ``served_entities``: tokens and requester land,
#: model identity is NULL and serving_mode is 'unknown', so GOLD withholds every $/token
#: claim. Partial measurement, honestly labelled — better than nothing and better than a guess.
#: ``none`` — the pull is skipped entirely.
ServingAvailability = Literal["full", "usage_only", "none"]

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
    """Classic vs serverless from the SKU name (see :class:`ComputeClass`)."""
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


def _opt_bool(value: object) -> bool | None:
    """Parse an optional boolean (warehouse values arrive as 'true'/'false' strings).

    Returns None for anything unrecognized rather than defaulting to False — for a config
    flag like ``scale_to_zero_enabled`` the difference between "measured as off" and
    "unmeasured" decides whether a rule may fire, so a coerced False would invent a finding.
    """
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    text = str(value).strip().lower()
    if text in ("true", "t", "1"):
        return True
    if text in ("false", "f", "0"):
        return False
    return None


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
        self._serving_availability: ServingAvailability | None = None  # probed lazily
        # (window, rows) — one warehouse round trip shared by fetch_ai_usage and the
        # endpoint EfficiencyRecords the runner asks for in a separate phase.
        self._ai_usage_cache: tuple[IngestWindow, list[dict[str, Any]]] | None = None
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
        # Serving endpoints join the same plane, so `idle`/`failed` fire on them with no new
        # rule. Yields nothing when system.serving is unavailable — see the method docstring
        # for why that is what keeps `idle`'s measured-zero invariant structural.
        yield from self._fetch_endpoint_efficiency(window)

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

    # ── Compute instances (the backing-compute plane) ────────────────────────
    def fetch_compute_instances(self, window: IngestWindow) -> Iterator[ComputeInstanceRecord]:
        """Yield aggregated ComputeInstanceRecord rows (cluster × instance × month).

        Own vendored query (``sql/databricks_compute_instances.sql``) against
        ``system.compute.node_timeline`` — pure metadata, no cost/waste semantics. This is
        what lets the AWS EC2 bill be labelled with the Databricks cluster behind it (see
        docs/design/backing-compute.md). Classic compute only: node_timeline has no rows
        for serverless SQL warehouses, serverless jobs or DLT serverless pipelines.
        """
        sql = self._render_compute_instances_query(window)
        fetched = mapped = 0
        for row in self._execute(sql):
            fetched += 1
            record = self._to_compute_instance(row)
            if record is not None:
                mapped += 1
                yield record
        logger.info(
            "databricks_compute_instances_done", rows_fetched=fetched, rows_mapped=mapped
        )

    def _render_compute_instances_query(self, window: IngestWindow) -> str:
        sql = _COMPUTE_INSTANCES_QUERY_PATH.read_text()
        return (
            sql.replace(":start_date", f"'{window.start}'")
            .replace(":end_date", f"'{window.end}'")
        )

    @staticmethod
    def _to_compute_instance(row: dict[str, Any]) -> ComputeInstanceRecord | None:
        """Map one aggregation row → ComputeInstanceRecord (values arrive as strings/None)."""
        charge_month = row.get("charge_month")
        cluster_id = row.get("cluster_id")
        instance_id = row.get("instance_id")
        if not charge_month or not cluster_id or not instance_id:
            return None
        return ComputeInstanceRecord(
            provider_name="Databricks",
            charge_month=date.fromisoformat(str(charge_month)[:10]),
            cluster_id=str(cluster_id),
            cluster_name=row.get("cluster_name") or None,
            owner_user=row.get("owner_user") or None,
            instance_id=str(instance_id),
            is_driver=_opt_bool(row.get("is_driver")),
            node_type=row.get("node_type") or None,
            x_source_connector="databricks",
        )

    # ── AI serving usage (the token plane) ──────────────────────────────────
    def _resolve_serving_tables(self) -> ServingAvailability:
        """Probe ``system.serving`` once and decide how much of the AI pull can run.

        Three rungs rather than all-or-nothing, because partial telemetry is genuinely
        useful and silence is not: without ``served_entities`` we still get real token
        counts and real requester identities, we just don't know the model or the serving
        mode — and because ``serving_mode`` then stays ``'unknown'``, GOLD withholds every
        $/token claim automatically. The alternative (guessing a mode) would put a
        fabricated rate on a real endpoint.

        A probe failure is treated as absent, not fatal: the probe shares the auth/warehouse
        path with the cost query, so if the connection is broken the cost pull will surface
        the real error — this pull must not be the thing that aborts the ingest.
        """
        if self._serving_availability is not None:
            return self._serving_availability

        def _present(table_name: str) -> bool:
            try:
                return bool(self._execute(_serving_probe(table_name)))
            except ConnectorError as exc:
                logger.warning("databricks_serving_probe_failed", table=table_name, error=str(exc))
                return False

        usage = _present("endpoint_usage")
        entities = _present("served_entities") if usage else False

        if usage and entities:
            self._serving_availability = "full"
            logger.info("databricks_serving_tables_found", table=_SERVING_USAGE_TABLE)
        elif usage:
            self._serving_availability = "usage_only"
            logger.warning(
                "databricks_serving_entities_unavailable",
                table=_SERVED_ENTITIES_TABLE,
                detail="token counts and requester identity will land, but model identity "
                "and serving mode will not — every $/token figure stays NULL rather than "
                "being derived from an unknown billing mode",
            )
        else:
            self._serving_availability = "none"
            logger.warning(
                "databricks_ai_usage_unavailable",
                table=_SERVING_USAGE_TABLE,
                detail="no serving system tables; AI token/user attribution is skipped. "
                "system.serving is a Public Preview schema — enable it for this account to "
                "populate the AI Costs tab's token panels. AI *cost* is unaffected: it comes "
                "from the billing data and is already complete.",
            )
        return self._serving_availability

    def _render_ai_usage_query(self, window: IngestWindow, *, entities: bool) -> str:
        """Render the AI-usage query, stubbing ``served_entities`` when it's unavailable.

        The ``usage_only`` rung replaces the CTE with a typed, empty one rather than editing
        the SELECT list: the LEFT JOIN then matches nothing, every ``e.*`` column comes back
        NULL, and ``serving_mode`` falls through the CASE ladder to ``'unknown'`` — exactly
        the degraded-but-honest shape we want, with no second copy of the projection to keep
        in sync.
        """
        sql = _AI_USAGE_QUERY_PATH.read_text()
        sql = sql.replace(":account_prices", f"'{self._resolve_account_prices()}'")
        if not entities:
            stub = (
                "SELECT\n"
                "    CAST(NULL AS STRING) AS served_entity_id,\n"
                "    CAST(NULL AS STRING) AS endpoint_id,\n"
                "    CAST(NULL AS STRING) AS endpoint_name,\n"
                "    CAST(NULL AS STRING) AS entity_type,\n"
                "    CAST(NULL AS STRING) AS entity_name,\n"
                "    CAST(NULL AS STRING) AS entity_version,\n"
                "    CAST(NULL AS STRING) AS workload_size,\n"
                "    CAST(NULL AS STRING) AS workload_type,\n"
                "    CAST(NULL AS BOOLEAN) AS scale_to_zero_enabled,\n"
                "    CAST(NULL AS DOUBLE) AS min_provisioned_throughput,\n"
                "    CAST(NULL AS DOUBLE) AS max_provisioned_throughput\n"
                "  WHERE 1 = 0"
            )
            start = sql.index("entities AS (")
            end = sql.index("),\nreq AS (")
            sql = f"{sql[:start]}entities AS (\n  {stub}\n{sql[end:]}"
        return sql.replace(":start_date", f"'{window.start}'").replace(
            ":end_date", f"'{window.end}'"
        )

    def _ai_usage_rows(self, window: IngestWindow) -> list[dict[str, Any]]:
        """Raw AI-usage rows for *window*, cached per window on the instance.

        ``fetch_ai_usage`` and ``_fetch_endpoint_efficiency`` both need this result and the
        runner calls them in separate phases on the SAME connector instance, so caching turns
        two warehouse round trips into one. Safe to hold in memory for the same reason the
        other ``_execute``-based pulls are: the result is pre-aggregated at source (endpoints
        × models × requesters × months — hundreds to low thousands of rows), nowhere near the
        vectorized cost path's scale.
        """
        if self._ai_usage_cache is not None and self._ai_usage_cache[0] == window:
            return self._ai_usage_cache[1]

        availability = self._resolve_serving_tables()
        rows: list[dict[str, Any]] = []
        if availability != "none":
            sql = self._render_ai_usage_query(window, entities=availability == "full")
            rows = list(self._execute(sql))
        self._ai_usage_cache = (window, rows)
        return rows

    def fetch_storage_locations(self, window: IngestWindow) -> Iterator[StorageLocationRecord]:
        """Unity Catalog's storage-location map: which cloud object-storage URLs back
        this metastore, its catalogs, and its external locations.

        Metadata only — no cost, no utilization. This is what lets AWS's S3 bill be
        labelled with the Databricks storage behind it; the two are never summed (see
        ``docs/design/backing-storage.md`` and CLAUDE.md's "No cross-provider cost join").

        Pure REST (``metastores.summary`` / ``catalogs.list`` / ``external_locations.list``)
        — **no running SQL warehouse required**, unlike every other pull in this
        connector, so the map still refreshes on a workspace whose warehouse is stopped.

        Each listing is independently best-effort: a token with permission to see
        external locations but not the metastore summary still yields the external
        locations. Losing one source degrades coverage; it doesn't blank the map.

        ``window`` is ignored — UC exposes only current state, so this is a snapshot
        stamped with the month it ran in, the same call ``_fetch_table_inventory`` makes
        for the same reason.

        Deliberately skipped: ``schemas.list()`` is one REST call *per catalog* (an N+1
        against a large metastore) and a schema's managed location is virtually always
        under its catalog's root, so it would add bucket coverage almost never.
        ``tables.list()`` is thousands of calls for locations under those same roots.
        ponytail: revisit if a real account turns out to put schemas on their own buckets.
        """
        del window  # snapshot, not a window — see the docstring
        month = date.today().replace(day=1)
        seen: set[tuple[str, str, str]] = set()
        records: list[StorageLocationRecord] = []

        for kind, name, url, read_only, credential in self._storage_location_sources():
            if not url:
                continue
            key = (kind, name, url)
            # A catalog whose storage_root equals the metastore root is common; counting
            # it twice would inflate the tab's "N Unity Catalog locations" per bucket.
            if key in seen:
                continue
            seen.add(key)
            scheme, provider, bucket, prefix = _parse_storage_url(url)
            records.append(
                StorageLocationRecord(
                    provider_name="Databricks",
                    snapshot_month=month,
                    location_kind=kind,
                    location_name=name,
                    url=url,
                    scheme=scheme,
                    cloud_provider_name=provider,
                    bucket_name=bucket,
                    key_prefix=prefix,
                    is_read_only=read_only,
                    credential_name=credential,
                    x_source_connector=self.name,
                )
            )

        buckets = {r.bucket_name for r in records if r.bucket_name}
        logger.info(
            "databricks_storage_locations_done", locations=len(records), buckets=len(buckets)
        )
        yield from records

    def _metastore_roots(
        self,
    ) -> Iterator[tuple[str, str, str | None, bool | None, str | None]]:
        """Every metastore's storage root, preferring ``list()`` over ``summary()``.

        This ladder is load-bearing, not defensive padding. ``metastores.summary()`` returns
        **only the metastore assigned to the workspace this connector points at** — so on an
        account with a prd and a dev metastore attached to different workspaces, summary()
        silently reports one and the other's bucket lands in `unmapped` looking like it isn't
        Databricks storage at all. Confirmed on a real account: the dev metastore's bucket was
        missing entirely until this changed.

        ``metastores.list()`` returns all of them but is **admin-only**, so a non-admin token
        must still get its own workspace's metastore rather than nothing. Hence: try the
        complete source, fall back to the partial one, and log which rung was used so a
        surprising coverage number is explainable after the fact.
        """
        try:
            metastores = list(self._client.metastores.list())
        except Exception as exc:  # noqa: BLE001 - admin-only; fall back, don't fail
            logger.info(
                "databricks_metastores_list_unavailable",
                error=str(exc),
                hint="non-admin token: falling back to this workspace's metastore only",
            )
        else:
            found = False
            for meta in metastores:
                if meta.storage_root and meta.name:
                    found = True
                    yield ("metastore_root", meta.name, meta.storage_root, None, None)
            if found:
                logger.info("databricks_metastore_roots", source="list", count=len(metastores))
                return

        try:
            summary = self._client.metastores.summary()
        except Exception as exc:  # noqa: BLE001 - one source of several; degrade, don't fail
            logger.warning("databricks_metastore_summary_failed", error=str(exc))
            return
        # A workspace with no metastore assigned returns nothing useful here; treat that as
        # "this source contributed no locations", not an error.
        if summary is not None and summary.storage_root:
            logger.info("databricks_metastore_roots", source="summary", count=1)
            yield (
                "metastore_root",
                summary.name or "(metastore)",
                summary.storage_root,
                None,
                None,
            )

    def _storage_location_sources(
        self,
    ) -> Iterator[tuple[str, str, str | None, bool | None, str | None]]:
        """``(location_kind, location_name, url, is_read_only, credential_name)`` from each
        Unity Catalog surface, each guarded on its own so one missing grant can't blank
        the whole map."""
        yield from self._metastore_roots()

        try:
            catalogs = list(self._client.catalogs.list())
        except Exception as exc:  # noqa: BLE001
            logger.warning("databricks_catalogs_list_failed", error=str(exc))
        else:
            for catalog in catalogs:
                root = catalog.storage_root or catalog.storage_location
                if not (root and catalog.name):
                    continue
                # MANAGED_CATALOG is storage Databricks provisioned; every other type
                # (FOREIGN_CATALOG for a Glue/Hive federation, DELTASHARING, SYSTEM) points
                # at data that already existed elsewhere. Both are recorded, but only the
                # managed kind is costed — see 065_gold_storage.sql. Confirmed on a real
                # account: a federated Glue catalog's storage_location was a shared
                # data-lake bucket costing 5x the managed catalogs combined, so counting it
                # would have charged another team's pipeline to Databricks.
                ctype = catalog.catalog_type.value if catalog.catalog_type else ""
                kind = "catalog" if ctype == "MANAGED_CATALOG" else "foreign_catalog"
                yield (kind, catalog.name, root, None, None)

        try:
            locations = list(self._client.external_locations.list())
        except Exception as exc:  # noqa: BLE001
            logger.warning("databricks_external_locations_list_failed", error=str(exc))
        else:
            for loc in locations:
                if loc.url and loc.name:
                    yield (
                        "external_location",
                        loc.name,
                        loc.url,
                        loc.read_only,
                        loc.credential_name,
                    )

    def fetch_ai_usage(self, window: IngestWindow) -> Iterator[AiUsageRecord]:
        """Yield aggregated AiUsageRecord rows (endpoint × model × requester × project × month).

        Measurement only — token/request volume, no dollar figure (the endpoint's spend stays
        canonical in the FOCUS plane; the join happens in GOLD). Skipped entirely when
        ``system.serving`` isn't available, which is what keeps the ``idle`` waste rule
        honest downstream: an unmeasured endpoint produces no row at all, so a measured zero
        can never be confused with silence.
        """
        fetched = mapped = 0
        for row in self._ai_usage_rows(window):
            fetched += 1
            record = self._to_ai_usage(row)
            if record is not None:
                mapped += 1
                yield record
        logger.info("databricks_ai_usage_done", rows_fetched=fetched, rows_mapped=mapped)

    def _fetch_endpoint_efficiency(self, window: IngestWindow) -> Iterator[EfficiencyRecord]:
        """Endpoint-grain EfficiencyRecords, so serving joins the existing waste plane.

        This is the "add rows, not views" payoff: ``idle``
        (``activity_count = 0 AND billed_cost > 0``) and ``failed``
        (``coalesce(failed_cost, 0) > 0``) are entity-type-agnostic, so an endpoint row fires
        them with no new rule and inherits ``waste_by_owner_month``,
        ``waste_resolution_month`` and the Efficiency & Waste tab for free.

        ``utilization_pct`` stays None — an endpoint has no CPU% and its waste is
        idle-provisioned capacity, not underutilization (055_gold_utilization.sql lists
        'endpoint' as not_applicable for exactly this reason). ``activity_count`` is the
        measured request count, and because this method yields nothing at all when the serving
        tables are unavailable, an unmeasured endpoint has NO row — so ``idle``'s "measured
        zero, never NULL" invariant holds structurally rather than by predicate.
        """
        rows = self._ai_usage_rows(window)
        if not rows:
            return

        # Fold the (endpoint × model × requester) grain up to (endpoint × month).
        by_endpoint: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            endpoint_id = row.get("endpoint_id")
            charge_month = row.get("charge_month")
            if not endpoint_id or not charge_month:
                continue
            key = (str(endpoint_id), str(charge_month)[:10])
            agg = by_endpoint.setdefault(
                key,
                {
                    "endpoint_name": row.get("endpoint_name") or None,
                    "serving_mode": row.get("serving_mode") or "unknown",
                    "requests": 0,
                    "error_requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "error_tokens": 0,
                    # MAX, not SUM: these are endpoint-month scalars fanned across the
                    # endpoint's several requester rows (see the query's trailing comment).
                    "billed_cost": 0.0,
                    "dbu_quantity": 0.0,
                    "tag_count": None,
                    "scale_to_zero_enabled": _opt_bool(row.get("scale_to_zero_enabled")),
                    "workload_type": row.get("workload_type") or None,
                    "workload_size": row.get("workload_size") or None,
                    "models": set(),
                },
            )
            agg["requests"] += _opt_int(row.get("request_count")) or 0
            agg["error_requests"] += _opt_int(row.get("error_request_count")) or 0
            agg["input_tokens"] += _opt_int(row.get("input_tokens")) or 0
            agg["output_tokens"] += _opt_int(row.get("output_tokens")) or 0
            agg["error_tokens"] += (_opt_int(row.get("error_input_tokens")) or 0) + (
                _opt_int(row.get("error_output_tokens")) or 0
            )
            agg["billed_cost"] = max(
                agg["billed_cost"], _opt_float(row.get("endpoint_billed_cost")) or 0.0
            )
            agg["dbu_quantity"] = max(
                agg["dbu_quantity"], _opt_float(row.get("endpoint_dbu_quantity")) or 0.0
            )
            tag_count = _opt_int(row.get("endpoint_tag_count"))
            if tag_count is not None:
                agg["tag_count"] = max(agg["tag_count"] or 0, tag_count)
            if row.get("model_name"):
                agg["models"].add(str(row["model_name"]))

        for (endpoint_id, charge_month), agg in by_endpoint.items():
            total_tokens = int(agg["input_tokens"]) + int(agg["output_tokens"])
            billed_cost = float(agg["billed_cost"])
            cause: dict[str, Any] = {
                "serving_mode": agg["serving_mode"],
                "input_tokens": agg["input_tokens"],
                "output_tokens": agg["output_tokens"],
                "total_tokens": total_tokens,
                "error_tokens": agg["error_tokens"],
                "request_count": agg["requests"],
                "error_request_count": agg["error_requests"],
                "model_count": len(agg["models"]),
                "scale_to_zero_enabled": agg["scale_to_zero_enabled"],
                "workload_type": agg["workload_type"],
                "workload_size": agg["workload_size"],
                "tag_count": agg["tag_count"],
            }
            if agg["requests"]:
                cause["error_rate_pct"] = round(
                    100.0 * int(agg["error_requests"]) / int(agg["requests"]), 2
                )
            # failed_cost ONLY where tokens are the meter. On an hourly-billed endpoint a
            # failed request consumes no separately-priced token, so a token-share split of
            # its cost would invent a dollar figure — the key stays absent, which leaves
            # failed_cost NULL and stops the `failed` rule firing at all. Same discipline as
            # GOLD's cost_allocation_basis.
            if agg["serving_mode"] == "pay_per_token" and total_tokens and billed_cost:
                cause["failed_cost"] = round(
                    billed_cost * int(agg["error_tokens"]) / total_tokens, 2
                )
            yield EfficiencyRecord(
                provider_name="Databricks",
                charge_month=date.fromisoformat(charge_month),
                entity_type=EntityType.ENDPOINT,
                entity_id=endpoint_id,
                entity_name=agg["endpoint_name"],
                owner_user=None,  # an endpoint has no run_as
                billed_cost=to_decimal(billed_cost),
                native_quantity=float(agg["dbu_quantity"]) or None,
                native_unit="DBU",
                utilization_pct=None,  # no CPU% for an endpoint — see the docstring
                activity_count=int(agg["requests"]),
                cause_detail=cause,
                x_source_connector="databricks",
            )

    @staticmethod
    def _to_ai_usage(row: dict[str, Any]) -> AiUsageRecord | None:
        """Map one aggregation row → AiUsageRecord (values arrive as strings/None).

        Skips a row with no endpoint identity or no month: without either it can't be joined
        to cost or placed in a partition, and a synthetic key would create a phantom endpoint.
        """
        charge_month = row.get("charge_month")
        endpoint_id = row.get("endpoint_id")
        if not charge_month or not endpoint_id:
            return None
        return AiUsageRecord(
            provider_name="Databricks",
            charge_month=date.fromisoformat(str(charge_month)[:10]),
            endpoint_id=str(endpoint_id),
            endpoint_name=row.get("endpoint_name") or None,
            served_entity_id=row.get("served_entity_id") or None,
            model_name=row.get("model_name") or None,
            model_version=row.get("model_version") or None,
            model_kind=row.get("model_kind") or None,
            # AiUsageRecord's validator normalizes anything unrecognized to 'unknown'.
            serving_mode=str(row.get("serving_mode") or "unknown"),
            requester=row.get("requester") or None,
            usage_context_project=row.get("usage_context_project") or None,
            scale_to_zero_enabled=_opt_bool(row.get("scale_to_zero_enabled")),
            workload_size=row.get("workload_size") or None,
            workload_type=row.get("workload_type") or None,
            min_provisioned_throughput=_opt_float(row.get("min_provisioned_throughput")),
            max_provisioned_throughput=_opt_float(row.get("max_provisioned_throughput")),
            request_count=_opt_int(row.get("request_count")) or 0,
            error_request_count=_opt_int(row.get("error_request_count")) or 0,
            input_tokens=_opt_int(row.get("input_tokens")) or 0,
            output_tokens=_opt_int(row.get("output_tokens")) or 0,
            error_input_tokens=_opt_int(row.get("error_input_tokens")) or 0,
            error_output_tokens=_opt_int(row.get("error_output_tokens")) or 0,
            total_duration_ms=_opt_int(row.get("total_duration_ms")) or 0,
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
        # Configurable so an org whose project-equivalent custom tag isn't literally
        # named "project" still gets a populated owner_project — see
        # DatabricksConfig.project_tag_key.
        project_tag_key = self._config.project_tag_key.replace("'", "''")
        sql = sql.replace(":project_tag_key", f"'{project_tag_key}'")
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
            # sql_warehouse/sql_warehouse_user only — the warehouse counterpart to a
            # cluster's auto_termination_minutes. See policy_rules.py's
            # warehouse_auto_stop category.
            "auto_stop_minutes": _opt_int(row.get("auto_stop_minutes")),
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


# ── Unity Catalog storage locations (the bucket map) ──────────────────────────────
# Which cloud object-storage URLs back this metastore. Pure metadata, no cost: it's what
# lets the *cloud provider's* storage bill be attributed to the platform on top of it,
# because Databricks' own bill (system.billing.usage) covers DBU compute only. See
# docs/design/backing-storage.md.

#: scheme -> the FOCUS ProviderName that bills for it. A scheme absent here is recorded
#: with cloud_provider_name NULL rather than guessed — an unmappable location is still
#: worth knowing about (it's part of the "locations pointing elsewhere" gap caption).
_STORAGE_SCHEME_PROVIDERS: dict[str, str] = {
    "s3": "AWS",
    "s3a": "AWS",
    "s3n": "AWS",
    "abfss": "Microsoft",
    "abfs": "Microsoft",
    "wasbs": "Microsoft",
    "gs": "Google Cloud",
}


def _parse_storage_url(url: str) -> tuple[str, str | None, str | None, str | None]:
    """``(scheme, cloud_provider_name, bucket_name, key_prefix)`` for a UC storage URL.

    ``key_prefix`` is ``None`` when the URL addresses the **bucket root**
    (``s3://b``, ``s3://b/``) and a real string otherwise. That distinction is the whole
    mapping-confidence signal downstream — the AWS bill's S3 ``ResourceId`` is
    bucket-grained, so a prefix-scoped location shares its bucket with whatever else
    lives there and its cost can only be an upper bound — so it is never collapsed to an
    empty string.

    Kept a pure module function (no SDK types, no ``self``) so the parsing can be tested
    directly over real-world URL shapes without stubbing a WorkspaceClient.

    For ``abfss://container@account.dfs.core.windows.net/path`` the "bucket" is
    ``container@account``: the container alone isn't unique across storage accounts, and
    a cost row would need both to be identified.
    """
    raw = (url or "").strip()
    if "://" not in raw:
        # dbfs:/... and anything else without an authority — record the scheme, claim no
        # bucket. DBFS in particular is the legacy workspace root, which is NOT a Unity
        # Catalog object and can't be resolved to a bucket here (a documented gap).
        scheme = raw.split(":", 1)[0].lower() if ":" in raw else "other"
        return (scheme or "other", None, None, None)

    scheme, rest = raw.split("://", 1)
    scheme = scheme.lower()
    provider = _STORAGE_SCHEME_PROVIDERS.get(scheme)
    authority, _, path = rest.partition("/")
    if not authority:
        return (scheme, provider, None, None)

    if scheme in ("abfss", "abfs", "wasbs") and "@" in authority:
        container, _, account = authority.partition("@")
        # Keep the storage-account host as delivered; only the container is normalized.
        bucket: str | None = f"{container}@{account}"
    else:
        bucket = authority

    prefix = path.strip("/") or None
    return (scheme, provider, bucket, prefix)
