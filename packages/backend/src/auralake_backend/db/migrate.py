"""Run alembic migrations programmatically — used by the init container."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig


def run() -> None:
    url = os.environ.get("AURALAKE_DATABASE_URL")
    if not url:
        print("ERROR: AURALAKE_DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    alembic_dir = Path(__file__).resolve().parents[3] / "alembic"
    alembic_ini = alembic_dir.parent / "alembic.ini"

    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.upgrade(cfg, "head")
