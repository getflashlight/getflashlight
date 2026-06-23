"""FOCUS BRONZE layer: meta + raw schemas, ingest_run + focus_record tables.

Revision ID: 001_focus_bronze
Revises:
Create Date: 2026-06-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001_focus_bronze"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMAS = ("meta", "raw", "silver", "gold")


def upgrade() -> None:
    for schema in _SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')

    op.create_table(
        "ingest_run",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("connector", sa.String, nullable=False, index=True),
        sa.Column("status", sa.String, nullable=False, server_default="running"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rows_ingested", sa.Integer, nullable=False, server_default="0"),
        sa.Column("detail", sa.String, nullable=True),
        schema="meta",
    )

    op.create_table(
        "focus_record",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("dedupe_key", sa.String, nullable=False),
        sa.Column("ingest_run_id", sa.Integer, sa.ForeignKey("meta.ingest_run.id")),
        # Provenance / accounts
        sa.Column("provider_name", sa.String, nullable=False),
        sa.Column("billing_account_id", sa.String, nullable=False),
        sa.Column("billing_account_name", sa.String),
        sa.Column("sub_account_id", sa.String),
        sa.Column("sub_account_name", sa.String),
        # Periods
        sa.Column("billing_period_start", sa.Date, nullable=False),
        sa.Column("billing_period_end", sa.Date, nullable=False),
        sa.Column("charge_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("charge_period_end", sa.DateTime(timezone=True), nullable=False),
        # Costs
        sa.Column("billing_currency", sa.String, nullable=False, server_default="USD"),
        sa.Column("billed_cost", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("effective_cost", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("list_cost", sa.Numeric(20, 6), nullable=False, server_default="0"),
        sa.Column("contracted_cost", sa.Numeric(20, 6), nullable=False, server_default="0"),
        # Classification
        sa.Column("charge_category", sa.String, nullable=False),
        sa.Column("charge_class", sa.String),
        sa.Column("charge_description", sa.String),
        # Service / SKU / location
        sa.Column("service_category", sa.String, nullable=False),
        sa.Column("service_name", sa.String, nullable=False),
        sa.Column("sku_id", sa.String),
        sa.Column("region_id", sa.String),
        # Resource
        sa.Column("resource_id", sa.String),
        sa.Column("resource_name", sa.String),
        sa.Column("resource_type", sa.String),
        # Usage
        sa.Column("consumed_quantity", sa.Float),
        sa.Column("consumed_unit", sa.String),
        # Tags + extensions
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("x_compute_class", sa.String, nullable=False, server_default="n/a"),
        sa.Column("x_focus_version", sa.String, nullable=False, server_default="1.1"),
        sa.Column("x_source_connector", sa.String, nullable=False, server_default="unknown"),
        sa.UniqueConstraint("dedupe_key", name="uq_focus_dedupe_key"),
        schema="raw",
    )

    op.create_index("ix_focus_provider", "focus_record", ["provider_name"], schema="raw")
    op.create_index("ix_focus_account", "focus_record", ["billing_account_id"], schema="raw")
    op.create_index("ix_focus_sub_account", "focus_record", ["sub_account_id"], schema="raw")
    op.create_index(
        "ix_focus_billing_period", "focus_record", ["billing_period_start"], schema="raw"
    )
    op.create_index(
        "ix_focus_charge_period", "focus_record", ["charge_period_start"], schema="raw"
    )
    op.create_index("ix_focus_service", "focus_record", ["service_name"], schema="raw")
    op.create_index("ix_focus_resource", "focus_record", ["resource_id"], schema="raw")
    op.create_index("ix_focus_charge_category", "focus_record", ["charge_category"], schema="raw")


def downgrade() -> None:
    op.drop_table("focus_record", schema="raw")
    op.drop_table("ingest_run", schema="meta")
    for schema in reversed(_SCHEMAS):
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
