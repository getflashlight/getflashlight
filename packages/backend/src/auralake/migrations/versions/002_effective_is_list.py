"""Add x_effective_is_list flag to raw.focus_record.

Marks rows whose EffectiveCost could only be sourced from list/rack rates (no
negotiated account-price table available), so consumers know discounts are not
reflected. See the Databricks connector's account-prices fallback.

Revision ID: 002_effective_is_list
Revises: 001_focus_bronze
Create Date: 2026-06-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "002_effective_is_list"
down_revision: str | None = "001_focus_bronze"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "focus_record",
        sa.Column(
            "x_effective_is_list",
            sa.Boolean,
            nullable=False,
            server_default=sa.false(),
        ),
        schema="raw",
    )


def downgrade() -> None:
    op.drop_column("focus_record", "x_effective_is_list", schema="raw")
