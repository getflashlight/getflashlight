"""Read-only queries over the published GOLD Parquet.

Shared by the MCP server and the dashboard. A single cached in-memory DuckDB
registers ``<group>.<view>`` over each ``gold/<group>/<view>.parquet`` — a schema
per provider group, plus ``shared`` for TCO (see
:func:`flashlight.lake.duck.register_gold`); the connection is rebuilt whenever a
publish changes the GOLD files, so reads are always fresh. Only GOLD is
registered, so raw/silver are simply not reachable.
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import duckdb

from flashlight.core.exceptions import FlashlightError
from flashlight.lake import duck, paths
from flashlight.transform.catalog import current_catalog_by_name, discover_provider_groups

MAX_LIMIT = 10_000

_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None
_signature: tuple[str, tuple[tuple[str, int], ...]] | None = None


class QueryError(FlashlightError):
    """Raised when a query is rejected (unknown view, unsafe SQL, etc.)."""


def _connection() -> duckdb.DuckDBPyConnection:
    global _conn, _signature
    # gold_signature() alone is relpath+mtime — it doesn't encode WHICH gold_dir, so two
    # different FLASHLIGHT_HOMEs with coincidentally-matching mtimes (e.g. two lakes
    # rebuilt back-to-back within the same process) can collide and silently serve one
    # lake's data for the other's queries. Pin the actual directory into the cache key.
    sig = (str(paths.gold_dir()), paths.gold_signature())
    if _conn is None or sig != _signature:
        if _conn is not None:
            _conn.close()
        _conn = duck.connect()
        duck.register_gold(_conn)
        # Structural hardening for this connection only (the ingest/transform
        # writers keep their own unrestricted duck.connect() — this one is the
        # only surface that runs untrusted SQL, via run_select). Views registered
        # above are `read_parquet('<disk path>')` reads, so this must NOT touch
        # enable_external_access/disabled_filesystems — either would break every
        # GOLD query, not just the untrusted-SQL path.
        _conn.execute("LOAD json")  # cause_detail is JSON — keep it queryable once autoload is off
        _conn.execute("SET autoinstall_known_extensions=false")
        _conn.execute("SET autoload_known_extensions=false")
        _conn.execute("SET lock_configuration=true")  # must be last — freezes these settings
        _signature = sig
    return _conn


def _jsonable(value: Any) -> Any:
    """Coerce DuckDB scalars into JSON-serializable values for MCP/Streamlit."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _rows(sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
    with _lock:
        cur = _connection().execute(sql, params) if params else _connection().execute(sql)
        columns = [d[0] for d in cur.description]
        return [
            {col: _jsonable(val) for col, val in zip(columns, row, strict=True)}
            for row in cur.fetchall()
        ]


def _split_sort_direction(order_by: str, descending: bool) -> tuple[str, bool]:
    """Accept a SQL-style ``"net_cost DESC"`` as well as a bare column name.

    ``order_by`` is a column and ``descending`` a separate flag, but an LLM
    naturally writes the ORDER BY clause it would write in SQL — confirmed live,
    a Databricks gpt-oss-20b sent ``order_by="net_cost DESC"``, got
    ``Cannot order by 'net_cost DESC'``, and gave up on the question entirely
    rather than retrying without it. The direction it asked for is unambiguous,
    so honour it instead of failing; an explicit ``descending=True`` still wins,
    since a caller that passed both meant the flag. Anything else (a real
    unknown column, an expression) still falls through to the catalog check and
    raises, so this widens the accepted spelling without widening what can run.
    """
    column, _, direction = order_by.strip().rpartition(" ")
    if column and direction.upper() in ("ASC", "DESC"):
        return column.strip(), descending or direction.upper() == "DESC"
    return order_by, descending


def query_view(
    view_name: str,
    limit: int = 1000,
    order_by: str | None = None,
    descending: bool = False,
    filters: dict[str, Any] | None = None,
    measures: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return rows from a catalogued GOLD view. Only known views are allowed.

    ``filters`` is an equality map over the view's dimensions/measures (e.g.
    ``{"charge_month": "2026-07-01", "compute_family": "job"}``) — the common case
    for an agent that already knows what it's looking for. A value may also be a
    list for an IN-match (e.g. ``{"charge_month": ["2026-06-01", "2026-07-01"]}``).
    For anything filters can't express (ranges, joins, aggregation), use ``run_sql``.

    ``measures`` narrows the returned columns to the view's dimensions plus this
    subset of its measures (default: every measure) — e.g. a view with several
    cost measures (net/gross/credit/...) returns all of them unless narrowed,
    which is too wide to chart as one dimension + one measure.
    """
    view = current_catalog_by_name().get(view_name)
    if view is None:
        raise QueryError(f"Unknown metric view: {view_name}")

    allowed = set(view.dimensions) | set(view.measures)
    limit = max(1, min(limit, MAX_LIMIT))
    if measures:
        unknown = [m for m in measures if m not in view.measures]
        if unknown:
            raise QueryError(
                f"Not a measure on {view_name}: {unknown}; measures are {list(view.measures)}"
            )
        columns = ", ".join([*view.dimensions, *measures])
        sql = f'SELECT {columns} FROM {view.name}'  # noqa: S608 - names validated against catalog
    else:
        sql = f'SELECT * FROM {view.name}'  # noqa: S608 - name validated against catalog
    params: list[Any] = []
    if filters:
        conditions = []
        for column, value in filters.items():
            if column not in allowed:
                raise QueryError(f"Cannot filter by {column!r} on {view_name}")
            if isinstance(value, list):
                if not value:
                    raise QueryError(f"Empty filter list for {column!r} on {view_name}")
                placeholders = ", ".join("?" for _ in value)
                # noqa: S608 - column validated against catalog
                conditions.append(f"{column} IN ({placeholders})")
                params.extend(value)
            else:
                conditions.append(f"{column} = ?")  # noqa: S608 - column validated against catalog
                params.append(value)
        sql += " WHERE " + " AND ".join(conditions)
    if order_by:
        order_by, descending = _split_sort_direction(order_by, descending)
        if order_by not in allowed:
            raise QueryError(f"Cannot order by {order_by!r} on {view_name}")
        sql += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
    sql += f" LIMIT {limit}"
    return _rows(sql, params)


def distinct_values(view_name: str, dimension: str, limit: int = 500) -> list[Any]:
    """Distinct values of one dimension on a catalogued view.

    Lets a caller discover valid filter values (tag keys/values, sku_id, compute_family,
    …) before calling ``query_view`` with a filter, instead of having to already know them.
    """
    view = current_catalog_by_name().get(view_name)
    if view is None:
        raise QueryError(f"Unknown metric view: {view_name}")
    if dimension not in view.dimensions:
        raise QueryError(
            f"{dimension!r} is not a dimension on {view_name}; dimensions are "
            f"{list(view.dimensions)}"
        )

    limit = max(1, min(limit, MAX_LIMIT))
    sql = (  # noqa: S608 - view/dimension validated against catalog
        f"SELECT DISTINCT {dimension} FROM {view.name} ORDER BY {dimension} LIMIT {limit}"
    )
    return [row[dimension] for row in _rows(sql)]


def run_select(sql: str, limit: int = 1000) -> list[dict[str, Any]]:
    """Run an ad-hoc read-only SELECT (for MCP). Rejects anything mutating."""
    cleaned = sql.strip().rstrip(";").strip()
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise QueryError("Only SELECT/WITH queries are permitted")
    if ";" in cleaned:
        raise QueryError("Multiple statements are not permitted")
    forbidden = (
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "create",
        "grant",
        "truncate",
        "attach",
        "detach",
        "pragma",
        "set",
        "reset",
        "call",
        "copy",
        "export",
        "install",
        "load",
        "checkpoint",
        "vacuum",
    )
    if any(re.search(rf"\b{kw}\b", lowered) for kw in forbidden):
        raise QueryError("Mutating keywords are not permitted")
    # Only the published GOLD groups are registered; reject raw/silver/meta (and the
    # old flat `gold.` schema) with a clear pointer to the per-provider schemas.
    for schema in re.findall(r"\b(raw|silver|meta|gold)\.", lowered):
        groups = discover_provider_groups() or ["<provider>"]
        hint = ", ".join(f"{g}.*" for g in [*groups, "shared"])
        raise QueryError(f"Schema {schema!r} is not queryable; use the metric schemas ({hint})")

    limit = max(1, min(limit, MAX_LIMIT))
    wrapped = f"SELECT * FROM ({cleaned}) AS _q LIMIT {limit}"  # noqa: S608 - validated above
    return _rows(wrapped)
