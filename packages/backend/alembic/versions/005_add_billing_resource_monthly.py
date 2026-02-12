"""Add billing_resource_monthly aggregation table.

Pre-aggregated billing by resource + month so the dashboard can
show top contributors at the right grain without on-the-fly
GROUP BY over raw billing_records.

Revision ID: 005
Revises: 004
Create Date: 2026-02-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str = "004"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_resource_monthly",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=False,
        ),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_key", sa.String(), nullable=False),
        sa.Column("resource_name", sa.String(), nullable=True),
        sa.Column("creator", sa.String(), nullable=True),
        sa.Column("dbu_usage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("resource_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        schema="inventory",
    )

    # Index for the primary query pattern: filter by SKU + month
    op.create_index(
        "ix_billing_resource_monthly_sku_month",
        "billing_resource_monthly",
        ["sku", "month"],
        schema="inventory",
    )

    # Index for delete-and-replace by connection_id
    op.create_index(
        "ix_billing_resource_monthly_connection_id",
        "billing_resource_monthly",
        ["connection_id"],
        schema="inventory",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_resource_monthly_connection_id",
        table_name="billing_resource_monthly",
        schema="inventory",
    )
    op.drop_index(
        "ix_billing_resource_monthly_sku_month",
        table_name="billing_resource_monthly",
        schema="inventory",
    )
    op.drop_table("billing_resource_monthly", schema="inventory")
