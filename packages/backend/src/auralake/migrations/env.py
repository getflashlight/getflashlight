"""Alembic environment — online migrations against the configured database."""

from __future__ import annotations

from alembic import context
from auralake.core.settings import get_settings

# Import models so their tables register on SQLModel.metadata.
from auralake.store import models  # noqa: F401
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

config = context.config
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = SQLModel.metadata


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {}) or {}
    section["sqlalchemy.url"] = get_settings().database_url
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
