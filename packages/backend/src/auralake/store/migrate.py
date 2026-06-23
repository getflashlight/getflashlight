"""Alembic migrations, applied automatically on service startup.

``upgrade_to_head()`` runs at the start of ``auralake serve`` and ``auralake
ingest`` when ``AURALAKE_AUTO_MIGRATE`` is true (the default). There is no separate
migrate command or init-container — the schema self-applies. Idempotent: running
against an already-current database is a no-op.
"""

from __future__ import annotations

import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from auralake.core.logging import get_logger
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
    """Apply all pending migrations. Optionally wait for the DB to be ready first.

    Run automatically on ``auralake serve`` / ``auralake ingest`` startup (gated by
    ``AURALAKE_AUTO_MIGRATE``); there is no separate migrate command.
    """
    if wait:
        _wait_for_db(get_settings().database_url)
    logger.info("migrate_start")
    command.upgrade(_alembic_config(), "head")
    logger.info("migrate_done")
