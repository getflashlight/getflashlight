"""Read-only queries over the published GOLD Parquet.

Shared by the MCP server and the dashboard. A single cached in-memory DuckDB
registers ``<group>.<view>`` over each ``gold/<group>/<view>.parquet`` — a schema
per provider group, plus ``shared`` for TCO (see
:func:`auralake.lake.duck.register_gold`); the connection is rebuilt whenever a
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

from auralake.core.exceptions import AuraLakeError
from auralake.lake import duck, paths
from auralake.transform.catalog import current_catalog_by_name, discover_provider_groups

MAX_LIMIT = 10_000

_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None
_signature: tuple[tuple[str, int], ...] | None = None


class QueryError(AuraLakeError):
    """Raised when a query is rejected (unknown view, unsafe SQL, etc.)."""


def _gold_signature() -> tuple[tuple[str, int], ...]:
    """Identity of the current GOLD files — rebuild the connection when it changes.

    Keyed on the path relative to ``gold/`` so two groups' identically-named files
    (e.g. ``aws/monthly_bill.parquet`` and ``databricks/monthly_bill.parquet``)
    don't collide in the signature.
    """
    gold = paths.gold_dir()
    return tuple(
        sorted(
            (p.relative_to(gold).as_posix(), p.stat().st_mtime_ns)
            for p in gold.glob("*/*.parquet")
        )
    )


def _connection() -> duckdb.DuckDBPyConnection:
    global _conn, _signature
    sig = _gold_signature()
    if _conn is None or sig != _signature:
        if _conn is not None:
            _conn.close()
        _conn = duck.connect()
        duck.register_gold(_conn)
        _signature = sig
    return _conn


def _jsonable(value: Any) -> Any:
    """Coerce DuckDB scalars into JSON-serializable values for MCP/Streamlit."""
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _rows(sql: str) -> list[dict[str, Any]]:
    with _lock:
        cur = _connection().execute(sql)
        columns = [d[0] for d in cur.description]
        return [
            {col: _jsonable(val) for col, val in zip(columns, row, strict=True)}
            for row in cur.fetchall()
        ]


def query_view(
    view_name: str,
    limit: int = 1000,
    order_by: str | None = None,
    descending: bool = False,
) -> list[dict[str, Any]]:
    """Return rows from a catalogued GOLD view. Only known views are allowed."""
    view = current_catalog_by_name().get(view_name)
    if view is None:
        raise QueryError(f"Unknown metric view: {view_name}")

    limit = max(1, min(limit, MAX_LIMIT))
    sql = f'SELECT * FROM {view.name}'  # noqa: S608 - name validated against catalog
    if order_by:
        allowed = set(view.dimensions) | set(view.measures)
        if order_by not in allowed:
            raise QueryError(f"Cannot order by {order_by!r} on {view_name}")
        sql += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
    sql += f" LIMIT {limit}"
    return _rows(sql)


def run_select(sql: str, limit: int = 1000) -> list[dict[str, Any]]:
    """Run an ad-hoc read-only SELECT (for MCP). Rejects anything mutating."""
    cleaned = sql.strip().rstrip(";").strip()
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise QueryError("Only SELECT/WITH queries are permitted")
    if ";" in cleaned:
        raise QueryError("Multiple statements are not permitted")
    forbidden = ("insert", "update", "delete", "drop", "alter", "create", "grant", "truncate")
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
