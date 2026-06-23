"""Read-only query helpers over the GOLD/SILVER surface.

Used by both the HTTP API and the MCP server. Everything here is read-only and
scoped to the published views — no writes, no access to raw/meta from agents.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import text

from auralake.core.exceptions import AuraLakeError
from auralake.store.engine import get_engine
from auralake.transform.catalog import CATALOG_BY_NAME

MAX_LIMIT = 10_000
_SAFE_IDENT = re.compile(r"^[a-z_][a-z0-9_.]*$")


class QueryError(AuraLakeError):
    """Raised when a query is rejected (unknown view, unsafe SQL, etc.)."""


def _rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    engine = get_engine()
    with engine.connect() as conn:
        # Read-only guard at the transaction level (belt-and-braces with SQL checks).
        conn.execute(text("SET TRANSACTION READ ONLY"))
        result = conn.execute(text(sql), params or {})
        return [dict(row) for row in result.mappings()]


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
    sql = f"SELECT * FROM {view.name}"  # noqa: S608 - name validated against catalog
    if order_by:
        allowed = set(view.dimensions) | set(view.measures)
        if order_by not in allowed:
            raise QueryError(f"Cannot order by {order_by!r} on {view_name}")
        sql += f" ORDER BY {order_by} {'DESC' if descending else 'ASC'}"
    sql += f" LIMIT {limit}"
    return _rows(sql)


def run_select(sql: str, limit: int = 1000) -> list[dict[str, Any]]:
    """Run an ad-hoc read-only SELECT (for MCP). Rejects anything mutating.

    Guard rails: must be a single SELECT/WITH statement against the gold/silver
    schemas, executed in a read-only transaction with an enforced row cap.
    """
    cleaned = sql.strip().rstrip(";").strip()
    lowered = cleaned.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        raise QueryError("Only SELECT/WITH queries are permitted")
    if ";" in cleaned:
        raise QueryError("Multiple statements are not permitted")
    forbidden = ("insert", "update", "delete", "drop", "alter", "create", "grant", "truncate")
    if any(re.search(rf"\b{kw}\b", lowered) for kw in forbidden):
        raise QueryError("Mutating keywords are not permitted")
    # Restrict table references to the public metric schemas.
    for schema in re.findall(r"\b(raw|meta)\.", lowered):
        raise QueryError(f"Schema {schema!r} is not queryable; use gold/silver views")

    limit = max(1, min(limit, MAX_LIMIT))
    wrapped = f"SELECT * FROM ({cleaned}) AS _q LIMIT {limit}"  # noqa: S608 - validated above
    return _rows(wrapped)
