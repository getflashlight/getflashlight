"""Add job_name column to billing_records.

Captures job_name from system.billing.usage usage_metadata so
name resolution can fall back to billing data when job profiles
are unavailable.

Revision ID: 004
Revises: 003
Create Date: 2026-02-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: str = "003"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "billing_records",
        sa.Column("job_name", sa.String(), nullable=True),
        schema="inventory",
    )


def downgrade() -> None:
    op.drop_column("billing_records", "job_name", schema="inventory")
