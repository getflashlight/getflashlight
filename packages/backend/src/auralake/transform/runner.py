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
    """Split a SQL file into individual statements (semicolon-terminated)."""
    statements = []
    for chunk in sql_text.split(";"):
        # Drop fragments that are only whitespace or SQL comments.
        code_lines = [
            line
            for line in chunk.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        if code_lines:
            statements.append(chunk.strip())
    return statements


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
