"""Read-only queries over the published GOLD Parquet.

Shared by the MCP server and the dashboard. A single cached in-memory DuckDB
registers ``gold.<view>`` over each ``gold/*.parquet`` (see
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
from auralake.transform.catalog import CATALOG_BY_NAME

MAX_LIMIT = 10_000

_lock = threading.Lock()
_conn: duckdb.DuckDBPyConnection | None = None
_signature: tuple[tuple[str, int], ...] | None = None


class QueryError(AuraLakeError):
    """Raised when a query is rejected (unknown view, unsafe SQL, etc.)."""


def _gold_signature() -> tuple[tuple[str, int], ...]:
    """Identity of the current GOLD files — rebuild the connection when it changes."""
    return tuple(
        sorted(
            (p.name, p.stat().st_mtime_ns) for p in paths.gold_dir().glob("*.parquet")
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
    view = CATALOG_BY_NAME.get(view_name)
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
    # Only GOLD is registered; reject explicit raw/silver/meta references for a clear error.
    for schema in re.findall(r"\b(raw|silver|meta)\.", lowered):
        raise QueryError(f"Schema {schema!r} is not queryable; use gold.* views")

    limit = max(1, min(limit, MAX_LIMIT))
    wrapped = f"SELECT * FROM ({cleaned}) AS _q LIMIT {limit}"  # noqa: S608 - validated above
    return _rows(wrapped)
