"""Apply SILVER views + GOLD materialized views, then refresh GOLD.

Subcommand: ``auralake transform [--rebuild]``. The ingest pipeline calls
``apply_views()`` right after upserting BRONZE, so GOLD is always current without a
separate step.

- SILVER are plain views (``CREATE OR REPLACE``) — cheap, recomputed on read.
- GOLD are materialized views, refreshed with ``REFRESH MATERIALIZED VIEW
  CONCURRENTLY`` so dashboards read precomputed data with no read downtime.

``CREATE MATERIALIZED VIEW IF NOT EXISTS`` won't alter an existing matview, so when a
definition in ``sql/`` changes, run ``--rebuild`` to drop + recreate. The normal path
also drops any leftover *plain* GOLD views, which auto-heals the one-time migration
from the previous (view-based) GOLD layer.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from auralake.core.logging import get_logger
from auralake.store.engine import get_engine

logger = get_logger(__name__)

SQL_DIR = Path(__file__).parent / "sql"


def _statements(sql_text: str) -> list[str]:
    """Split a SQL file into individual statements (semicolon-terminated).

    Line comments are stripped first, because a ``;`` inside a ``--`` comment must
    not be treated as a statement terminator. (Our SQL has no string literals or
    dollar-quoted bodies containing ``--`` or ``;``, so this is sufficient.)
    """
    decommented = []
    for line in sql_text.splitlines():
        idx = line.find("--")
        decommented.append(line[:idx] if idx != -1 else line)
    cleaned = "\n".join(decommented)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def _gold_matviews(conn: Connection) -> list[str]:
    rows = conn.execute(
        text("SELECT matviewname FROM pg_matviews WHERE schemaname = 'gold' ORDER BY matviewname")
    )
    return [r[0] for r in rows]


def _drop_stale_gold_views(conn: Connection) -> None:
    """Drop any leftover plain views in GOLD (it's materialized-only now)."""
    rows = conn.execute(text("SELECT viewname FROM pg_views WHERE schemaname = 'gold'"))
    for (name,) in rows.fetchall():
        conn.execute(text(f'DROP VIEW IF EXISTS gold."{name}" CASCADE'))
        logger.info("gold_stale_view_dropped", view=name)


def _apply_sql_files() -> None:
    files = sorted(SQL_DIR.glob("*.sql"))
    with get_engine().begin() as conn:
        _drop_stale_gold_views(conn)
        for path in files:
            for stmt in _statements(path.read_text()):
                conn.execute(text(stmt))
            logger.info("sql_applied", file=path.name)


def _refresh_gold() -> int:
    """REFRESH MATERIALIZED VIEW CONCURRENTLY each GOLD matview (cannot run in a txn)."""
    refreshed = 0
    with get_engine().connect() as base:
        conn = base.execution_options(isolation_level="AUTOCOMMIT")
        for name in _gold_matviews(conn):
            conn.execute(text(f'REFRESH MATERIALIZED VIEW CONCURRENTLY gold."{name}"'))
            refreshed += 1
            logger.info("gold_refreshed", view=name)
    return refreshed


def _drop_gold_matviews() -> None:
    with get_engine().begin() as conn:
        for name in _gold_matviews(conn):
            conn.execute(text(f'DROP MATERIALIZED VIEW IF EXISTS gold."{name}" CASCADE'))
            logger.info("gold_matview_dropped", view=name)


def apply_views(rebuild: bool = False) -> int:
    """Apply view definitions and refresh GOLD. Returns count of refreshed matviews.

    ``rebuild`` drops existing GOLD matviews first, so changed definitions in ``sql/``
    take effect (``CREATE ... IF NOT EXISTS`` alone would skip them).
    """
    if rebuild:
        _drop_gold_matviews()
    _apply_sql_files()
    return _refresh_gold()


