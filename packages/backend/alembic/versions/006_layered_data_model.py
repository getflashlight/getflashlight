"""Add layered data model: cleaned, enriched, aggregated schemas.

Adds unique constraints to existing raw tables, creates the
cluster_utilization_snapshots table, and builds Layer 2/3/4 tables
for the cleaned → enriched → aggregated data pipeline.

Revision ID: 006
Revises: 005
Create Date: 2026-03-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str = "005"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Create new schemas
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS cleaned")
    op.execute("CREATE SCHEMA IF NOT EXISTS enriched")
    op.execute("CREATE SCHEMA IF NOT EXISTS aggregated")

    # ------------------------------------------------------------------
    # Add missing columns and unique constraints to existing raw tables
    # ------------------------------------------------------------------

    # BillingRecord: add record_hash column + unique constraint
    op.add_column(
        "billing_records",
        sa.Column("record_hash", sa.String(), nullable=True),
        schema="inventory",
    )
    op.create_index(
        "ix_billing_records_record_hash",
        "billing_records",
        ["record_hash"],
        unique=True,
        schema="inventory",
    )

    # QueryPlan: add connection_id + unique constraint
    op.add_column(
        "query_plans",
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=True,
        ),
        schema="inventory",
    )
    op.create_unique_constraint(
        "uq_query_plans_connection_query",
        "query_plans",
        ["connection_id", "query_id"],
        schema="inventory",
    )

    # JobProfileRecord: add connection_id + unique constraint
    op.add_column(
        "job_profiles",
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=True,
        ),
        schema="inventory",
    )
    op.create_unique_constraint(
        "uq_job_profiles_connection_job",
        "job_profiles",
        ["connection_id", "job_id"],
        schema="inventory",
    )

    # InfraResourceMapping: add connection_id + unique constraint
    op.add_column(
        "infra_resource_mappings",
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=True,
        ),
        schema="inventory",
    )
    op.create_unique_constraint(
        "uq_infra_resource_mappings_connection_type_id",
        "infra_resource_mappings",
        ["connection_id", "platform_resource_type", "platform_resource_id"],
        schema="inventory",
    )

    # InfraCostSnapshot: add connection_id + unique constraint
    op.add_column(
        "infra_cost_snapshots",
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=True,
        ),
        schema="inventory",
    )
    op.create_unique_constraint(
        "uq_infra_cost_snapshots_connection_period_svc_res",
        "infra_cost_snapshots",
        ["connection_id", "period_start", "service", "resource_id"],
        schema="inventory",
    )

    # S3InventoryObject: add connection_id + unique constraint
    op.add_column(
        "s3_inventory_objects",
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=True,
        ),
        schema="inventory",
    )
    op.create_unique_constraint(
        "uq_s3_inventory_objects_connection_bucket_key",
        "s3_inventory_objects",
        ["connection_id", "bucket", "key"],
        schema="inventory",
    )

    # ------------------------------------------------------------------
    # New raw table: cluster_utilization_snapshots
    # ------------------------------------------------------------------
    op.create_table(
        "cluster_utilization_snapshots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=False,
        ),
        sa.Column("cluster_id", sa.String(), nullable=False),
        sa.Column("avg_cpu_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("avg_memory_percent", sa.Float(), nullable=False, server_default="0"),
        sa.Column("active_hours", sa.Float(), nullable=False, server_default="0"),
        sa.Column("idle_hours", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "connection_id",
            "cluster_id",
            "captured_at",
            name="uq_cluster_util_conn_cluster_captured",
        ),
        schema="inventory",
    )

    # ------------------------------------------------------------------
    # Layer 2: cleaned.billing_sku_day
    # ------------------------------------------------------------------
    op.create_table(
        "billing_sku_day",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=False,
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("resource_name", sa.String(), nullable=True),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("dbu_usage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "connection_id",
            "usage_date",
            "sku",
            "resource_type",
            "resource_id",
            name="uq_cleaned_billing_sku_day",
        ),
        schema="cleaned",
    )
    op.create_index(
        "ix_cleaned_billing_conn_date",
        "billing_sku_day",
        ["connection_id", "usage_date"],
        schema="cleaned",
    )
    op.create_index(
        "ix_cleaned_billing_sku_date",
        "billing_sku_day",
        ["sku", "usage_date"],
        schema="cleaned",
    )
    op.create_index(
        "ix_cleaned_billing_resource",
        "billing_sku_day",
        ["resource_type", "resource_id"],
        schema="cleaned",
    )

    # ------------------------------------------------------------------
    # Layer 3: enriched.billing_resource
    # ------------------------------------------------------------------
    op.create_table(
        "billing_resource",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=False,
        ),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("sku", sa.String(), nullable=False),
        sa.Column("resource_type", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=False),
        sa.Column("resource_name", sa.String(), nullable=True),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("dbu_usage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("record_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("creator", sa.String(), nullable=True),
        sa.Column("worker_node_type", sa.String(), nullable=True),
        sa.Column("num_workers", sa.Integer(), nullable=True),
        sa.Column("autoscale", sa.Boolean(), nullable=True),
        sa.Column("spot_enabled", sa.Boolean(), nullable=True),
        sa.Column("autotermination_minutes", sa.Integer(), nullable=True),
        sa.Column("warehouse_type", sa.String(), nullable=True),
        sa.Column("warehouse_size", sa.String(), nullable=True),
        sa.Column("compute_state", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "connection_id",
            "usage_date",
            "sku",
            "resource_type",
            "resource_id",
            name="uq_enriched_billing_resource",
        ),
        schema="enriched",
    )

    # ------------------------------------------------------------------
    # Layer 3: enriched.job_runs
    # ------------------------------------------------------------------
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("job_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("job_name", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        sa.Column("end_time", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("cluster_id", sa.String(), nullable=True),
        sa.Column("trigger", sa.String(), nullable=True),
        sa.Column("error_message", sa.String(), nullable=True),
        # From JobProfileRecord
        sa.Column("schedule_cron", sa.String(), nullable=True),
        sa.Column("instance_type", sa.String(), nullable=True),
        sa.Column("worker_count", sa.Integer(), nullable=True),
        sa.Column("avg_dbu_cost", sa.Float(), nullable=True),
        sa.Column("is_portable", sa.Boolean(), nullable=True),
        # From ComputeResourceRecord
        sa.Column("cluster_name", sa.String(), nullable=True),
        sa.Column("spot_enabled", sa.Boolean(), nullable=True),
        sa.Column("cluster_creator", sa.String(), nullable=True),
        # Estimated cost
        sa.Column("estimated_run_cost_usd", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "connection_id",
            "run_id",
            name="uq_enriched_job_runs",
        ),
        schema="enriched",
    )

    # ------------------------------------------------------------------
    # Layer 3: enriched.queries
    # ------------------------------------------------------------------
    op.create_table(
        "queries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("query_id", sa.String(), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("user_name", sa.String(), nullable=True),
        sa.Column("warehouse_id", sa.String(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("rows_produced", sa.Integer(), nullable=True),
        sa.Column("query_start_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("query_end_time_ms", sa.BigInteger(), nullable=True),
        # From QueryPlan
        sa.Column("has_plan", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("anti_patterns", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "anti_pattern_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("rows_scanned", sa.Integer(), nullable=True),
        sa.Column("bytes_read", sa.Integer(), nullable=True),
        sa.Column("shuffle_bytes", sa.Integer(), nullable=True),
        sa.Column("spill_bytes", sa.Integer(), nullable=True),
        # From ComputeResourceRecord
        sa.Column("warehouse_name", sa.String(), nullable=True),
        sa.Column("warehouse_type", sa.String(), nullable=True),
        sa.Column("warehouse_size", sa.String(), nullable=True),
        # From billing
        sa.Column("estimated_cost_usd", sa.Float(), nullable=True),
        # From QueryPlan / JobProfileRecord
        sa.Column("job_id", sa.String(), nullable=True),
        sa.Column("job_name", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "connection_id",
            "query_id",
            name="uq_enriched_queries",
        ),
        schema="enriched",
    )

    # ------------------------------------------------------------------
    # Layer 4: aggregated.billing_resource_monthly
    # ------------------------------------------------------------------
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
        sa.Column("workspace_id", sa.String(), nullable=True),
        sa.Column("dbu_usage", sa.Float(), nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("resource_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("avg_daily_dbu", sa.Float(), nullable=True),
        sa.Column("peak_daily_dbu", sa.Float(), nullable=True),
        sa.Column("active_days", sa.Integer(), nullable=True),
        sa.Column("spot_enabled", sa.Boolean(), nullable=True),
        sa.Column("worker_node_type", sa.String(), nullable=True),
        sa.Column("warehouse_type", sa.String(), nullable=True),
        sa.Column("prev_month_cost_usd", sa.Float(), nullable=True),
        sa.Column("cost_change_pct", sa.Float(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "connection_id",
            "month",
            "sku",
            "resource_type",
            "resource_key",
            name="uq_agg_billing_resource_monthly",
        ),
        schema="aggregated",
    )
    op.create_index(
        "ix_agg_billing_monthly_sku_month",
        "billing_resource_monthly",
        ["sku", "month"],
        schema="aggregated",
    )
    op.create_index(
        "ix_agg_billing_monthly_connection",
        "billing_resource_monthly",
        ["connection_id"],
        schema="aggregated",
    )

    # ------------------------------------------------------------------
    # Layer 4: aggregated.recommendation_summary
    # ------------------------------------------------------------------
    op.create_table(
        "recommendation_summary",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.Uuid(),
            sa.ForeignKey("core.provider_connections.id"),
            nullable=False,
        ),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("recommendation_type", sa.String(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "total_estimated_savings_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_actual_savings_usd",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("applied_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dismissed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "connection_id",
            "month",
            "recommendation_type",
            name="uq_agg_recommendation_summary",
        ),
        schema="aggregated",
    )


def downgrade() -> None:
    # Drop Layer 4
    op.drop_index(
        "ix_agg_billing_monthly_connection",
        table_name="billing_resource_monthly",
        schema="aggregated",
    )
    op.drop_index(
        "ix_agg_billing_monthly_sku_month",
        table_name="billing_resource_monthly",
        schema="aggregated",
    )
    op.drop_table("recommendation_summary", schema="aggregated")
    op.drop_table("billing_resource_monthly", schema="aggregated")

    # Drop Layer 3
    op.drop_table("queries", schema="enriched")
    op.drop_table("job_runs", schema="enriched")
    op.drop_table("billing_resource", schema="enriched")

    # Drop Layer 2
    op.drop_index(
        "ix_cleaned_billing_resource",
        table_name="billing_sku_day",
        schema="cleaned",
    )
    op.drop_index(
        "ix_cleaned_billing_sku_date",
        table_name="billing_sku_day",
        schema="cleaned",
    )
    op.drop_index(
        "ix_cleaned_billing_conn_date",
        table_name="billing_sku_day",
        schema="cleaned",
    )
    op.drop_table("billing_sku_day", schema="cleaned")

    # Drop new raw table
    op.drop_table("cluster_utilization_snapshots", schema="inventory")

    # Remove added constraints and columns
    op.drop_constraint(
        "uq_s3_inventory_objects_connection_bucket_key",
        "s3_inventory_objects",
        schema="inventory",
    )
    op.drop_column("s3_inventory_objects", "connection_id", schema="inventory")

    op.drop_constraint(
        "uq_infra_cost_snapshots_connection_period_svc_res",
        "infra_cost_snapshots",
        schema="inventory",
    )
    op.drop_column("infra_cost_snapshots", "connection_id", schema="inventory")

    op.drop_constraint(
        "uq_infra_resource_mappings_connection_type_id",
        "infra_resource_mappings",
        schema="inventory",
    )
    op.drop_column("infra_resource_mappings", "connection_id", schema="inventory")

    op.drop_constraint(
        "uq_job_profiles_connection_job",
        "job_profiles",
        schema="inventory",
    )
    op.drop_column("job_profiles", "connection_id", schema="inventory")

    op.drop_constraint(
        "uq_query_plans_connection_query",
        "query_plans",
        schema="inventory",
    )
    op.drop_column("query_plans", "connection_id", schema="inventory")

    op.drop_index(
        "ix_billing_records_record_hash",
        table_name="billing_records",
        schema="inventory",
    )
    op.drop_column("billing_records", "record_hash", schema="inventory")

    # Drop schemas
    op.execute("DROP SCHEMA IF EXISTS aggregated CASCADE")
    op.execute("DROP SCHEMA IF EXISTS enriched CASCADE")
    op.execute("DROP SCHEMA IF EXISTS cleaned CASCADE")
