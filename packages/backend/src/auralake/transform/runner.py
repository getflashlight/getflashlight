"""Apply SILVER/GOLD SQL views. Entry point: ``auralake-transform``.

The views are idempotent (CREATE OR REPLACE), so this is safe to run after every
ingest. SQL files in ``sql/`` are applied in lexical order (010_, 020_, 030_).
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from auralake.core.logging import get_logger, setup_logging
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


def apply_views() -> int:
    """Apply every SQL file in order. Returns the number of statements executed."""
    files = sorted(SQL_DIR.glob("*.sql"))
    count = 0
    engine = get_engine()
    with engine.begin() as conn:
        for path in files:
            for stmt in _statements(path.read_text()):
                conn.execute(text(stmt))
                count += 1
            logger.info("sql_applied", file=path.name)
    return count


def run() -> None:
    setup_logging()
    logger.info("transform_start")
    n = apply_views()
    logger.info("transform_done", statements=n)


if __name__ == "__main__":
    run()
