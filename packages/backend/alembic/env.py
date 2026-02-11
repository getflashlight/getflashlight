"""Alembic environment configuration for auralake."""

from __future__ import annotations

import os
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context

# Import all models so they register with SQLModel.metadata
from auralake_backend.db.models import (  # noqa: F401
    AnalysisRun,
    ApiKey,
    AuditLog,
    BillingRecord,
    ClusterPolicyRecord,
    CollectionRun,
    ComputeResourceRecord,
    ConsolidationGroupRecord,
    InfraCostSnapshot,
    InfraResourceMapping,
    JobProfileRecord,
    JobRunRecord,
    ProviderConnection,
    QueryHistoryRecord,
    QueryPlan,
    RecommendationRecord,
    S3InventoryObject,
    WorkerCursor,
)
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Allow AURALAKE_DATABASE_URL to override alembic.ini (used by init container
# and programmatic callers).
env_url = os.environ.get("AURALAKE_DATABASE_URL")
if env_url:
    config.set_main_option("sqlalchemy.url", env_url)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="core",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # Ensure schemas exist before Alembic tries to access the version table
        # in the core schema. This avoids a chicken-and-egg problem on first run.
        connection.execute(sa.text("CREATE SCHEMA IF NOT EXISTS core"))
        connection.execute(sa.text("CREATE SCHEMA IF NOT EXISTS inventory"))
        connection.commit()

        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            version_table_schema="core",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
