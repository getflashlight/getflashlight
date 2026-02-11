"""Add pricing_basis and savings tracking columns to recommendations.

Tracks whether savings estimates use list prices or negotiated
(discounted) rates, plus actual savings verification fields that
compare collected cost data before/after application.

Revision ID: 003
Revises: 002
Create Date: 2026-02-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str = "002"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column(
            "pricing_basis",
            sa.String(),
            nullable=False,
            server_default=sa.text("'list'"),
        ),
        schema="core",
    )
    op.add_column(
        "recommendations",
        sa.Column("baseline_monthly_cost_usd", sa.Float(), nullable=True),
        schema="core",
    )
    op.add_column(
        "recommendations",
        sa.Column("actual_monthly_savings_usd", sa.Float(), nullable=True),
        schema="core",
    )
    op.add_column(
        "recommendations",
        sa.Column("savings_verified_at", sa.DateTime(), nullable=True),
        schema="core",
    )


def downgrade() -> None:
    op.drop_column("recommendations", "savings_verified_at", schema="core")
    op.drop_column("recommendations", "actual_monthly_savings_usd", schema="core")
    op.drop_column("recommendations", "baseline_monthly_cost_usd", schema="core")
    op.drop_column("recommendations", "pricing_basis", schema="core")
