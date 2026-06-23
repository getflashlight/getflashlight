"""Alembic migrations. Entry point ``auralake-db-migrate`` and in-process helper.

Two ways migrations get applied:
  1. The dedicated ``migrate`` service in docker-compose (the init-container
     pattern) — the primary path; server/mcp wait for it to complete.
  2. ``upgrade_to_head()`` called on server startup when ``AURALAKE_AUTO_MIGRATE``
     is true — a fallback for deployments that don't run the migrate service.

Both are idempotent: running against an already-current database is a no-op.
"""

from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from auralake.core.logging import get_logger, setup_logging
from auralake.core.settings import get_settings

logger = get_logger(__name__)


def _alembic_config() -> Config:
    # Migrations ship inside the package (src/auralake/migrations) so they're
    # present in a --no-editable wheel. Resolve relative to this module, and build
    # the Config in code so we don't depend on a packaged alembic.ini.
    migrations_dir = Path(__file__).resolve().parent.parent / "migrations"
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations_dir))
    cfg.set_main_option("sqlalchemy.url", get_settings().database_url)
    return cfg


def _wait_for_db(database_url: str, attempts: int = 30, delay: float = 1.0) -> None:
    """Block until the database accepts connections (tolerates a starting DB)."""
    engine = create_engine(database_url)
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()
            return
        except Exception as exc:  # noqa: BLE001 - retry until ready
            last_err = exc
            logger.info("waiting_for_db", attempt=i + 1, attempts=attempts)
            time.sleep(delay)
    engine.dispose()
    raise RuntimeError(f"Database not reachable after {attempts} attempts: {last_err}")


def upgrade_to_head(wait: bool = True) -> None:
    """Apply all pending migrations. Optionally wait for the DB to be ready first."""
    if wait:
        _wait_for_db(get_settings().database_url)
    command.upgrade(_alembic_config(), "head")


def run() -> None:
    setup_logging()
    logger.info("migrate_start")
    upgrade_to_head()
    logger.info("migrate_done")


if __name__ == "__main__":
    run()
