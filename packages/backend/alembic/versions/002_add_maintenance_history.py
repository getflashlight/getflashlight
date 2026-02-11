"""Add maintenance history columns to unity_catalog_tables.

Adds 9 columns from DESCRIBE HISTORY data: last_optimized_at,
last_vacuumed_at, optimize/vacuum counts, compaction metrics,
clustering flags, and a separate history_error field.

Revision ID: 002
Revises: 001
Create Date: 2026-02-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str = "001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "unity_catalog_tables",
        sa.Column("last_optimized_at", sa.DateTime(), nullable=True),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column("last_vacuumed_at", sa.DateTime(), nullable=True),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column("optimize_count_30d", sa.Integer(), nullable=True),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column("vacuum_count_30d", sa.Integer(), nullable=True),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column("last_optimize_removed_files", sa.Integer(), nullable=True),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column("last_optimize_added_bytes", sa.BigInteger(), nullable=True),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column(
            "uses_liquid_clustering",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column(
            "uses_zordering",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        schema="inventory",
    )
    op.add_column(
        "unity_catalog_tables",
        sa.Column("history_error", sa.String(), nullable=True),
        schema="inventory",
    )


def downgrade() -> None:
    op.drop_column("unity_catalog_tables", "history_error", schema="inventory")
    op.drop_column("unity_catalog_tables", "uses_zordering", schema="inventory")
    op.drop_column("unity_catalog_tables", "uses_liquid_clustering", schema="inventory")
    op.drop_column("unity_catalog_tables", "last_optimize_added_bytes", schema="inventory")
    op.drop_column("unity_catalog_tables", "last_optimize_removed_files", schema="inventory")
    op.drop_column("unity_catalog_tables", "vacuum_count_30d", schema="inventory")
    op.drop_column("unity_catalog_tables", "optimize_count_30d", schema="inventory")
    op.drop_column("unity_catalog_tables", "last_vacuumed_at", schema="inventory")
    op.drop_column("unity_catalog_tables", "last_optimized_at", schema="inventory")
