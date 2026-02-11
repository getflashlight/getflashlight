"""Initial schema with core and inventory PostgreSQL schemas.

Creates all tables organized into two schemas:
- core: application state, credentials, analysis outputs
- inventory: raw collected infrastructure data

Revision ID: 001
Revises:
Create Date: 2026-02-10
"""

from __future__ import annotations

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "001"
down_revision: str | None = None
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # core schema tables
    # ------------------------------------------------------------------

    # provider_connections (referenced by many other tables)
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("encrypted_credentials", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "name"),
        schema="core",
    )

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )

    op.create_table(
        "analysis_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analyzer_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("config_snapshot", sa.JSON(), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )

    op.create_table(
        "consolidation_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("group_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("recommended_cluster_config", sa.JSON(), nullable=True),
        sa.Column("recommended_dab_changes", sa.JSON(), nullable=True),
        sa.Column("estimated_monthly_savings_usd", sa.Float(), nullable=False),
        sa.Column("job_count", sa.Integer(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("pr_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )

    op.create_table(
        "recommendations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("risk_level", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("resource_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("resource_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("title", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("current_state", sa.JSON(), nullable=True),
        sa.Column("recommended_state", sa.JSON(), nullable=True),
        sa.Column("estimated_monthly_savings_usd", sa.Float(), nullable=False),
        sa.Column("savings_confidence", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=True),
        sa.Column("pr_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["core.analysis_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("recommendation_id", sa.Uuid(), nullable=True),
        sa.Column("action_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("resource_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("automation_level", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("before_state", sa.JSON(), nullable=True),
        sa.Column("after_state", sa.JSON(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("error_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("pr_url", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("executed_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("executed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["recommendation_id"], ["core.recommendations.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )

    op.create_table(
        "collection_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("trigger", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("worker_statuses", sa.JSON(), nullable=True),
        sa.Column("error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="core",
    )

    op.create_table(
        "worker_cursors",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("worker_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cursor_value", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "worker_name"),
        schema="core",
    )

    # ------------------------------------------------------------------
    # inventory schema tables
    # ------------------------------------------------------------------

    op.create_table(
        "billing_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("sku", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("cluster_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("warehouse_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("endpoint_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("pipeline_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("notebook_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("dbu_usage", sa.Float(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="inventory",
    )

    op.create_table(
        "job_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("job_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("schedule_cron", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("avg_duration_minutes", sa.Float(), nullable=False),
        sa.Column("avg_dbu_cost", sa.Float(), nullable=False),
        sa.Column("instance_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("worker_count", sa.Integer(), nullable=False),
        sa.Column("spark_config", sa.JSON(), nullable=True),
        sa.Column("data_sources", sa.JSON(), nullable=True),
        sa.Column("databricks_features_used", sa.JSON(), nullable=True),
        sa.Column("dab_file_path", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("dab_job_key", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("is_portable", sa.Boolean(), nullable=False),
        sa.Column("consolidation_group_id", sa.Uuid(), nullable=True),
        sa.Column("last_analyzed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["consolidation_group_id"], ["core.consolidation_groups.id"]),
        sa.PrimaryKeyConstraint("id"),
        schema="inventory",
    )

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("run_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("start_time", sa.DateTime(), nullable=True),
        sa.Column("end_time", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("cluster_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("trigger", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("error_message", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "run_id"),
        schema="inventory",
    )

    op.create_table(
        "query_history",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("query_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("query_text", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("user_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("warehouse_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("rows_produced", sa.Integer(), nullable=True),
        sa.Column("query_start_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("query_end_time_ms", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "query_id"),
        schema="inventory",
    )

    op.create_table(
        "query_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("query_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("job_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("job_run_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("cluster_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("query_text", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("logical_plan", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("physical_plan", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("parsed_plan", sa.JSON(), nullable=True),
        sa.Column("anti_patterns", sa.JSON(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("rows_scanned", sa.Integer(), nullable=True),
        sa.Column("bytes_read", sa.Integer(), nullable=True),
        sa.Column("shuffle_bytes", sa.Integer(), nullable=True),
        sa.Column("spill_bytes", sa.Integer(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="inventory",
    )

    op.create_table(
        "unity_catalog_tables",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_name", sa.String(), nullable=False),
        sa.Column("schema_name", sa.String(), nullable=False),
        sa.Column("table_name", sa.String(), nullable=False),
        sa.Column("full_name", sa.String(), nullable=False),
        sa.Column("table_type", sa.String(), nullable=False),
        sa.Column("data_format", sa.String(), nullable=True),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("num_files", sa.Integer(), nullable=True),
        sa.Column("owner", sa.String(), nullable=True),
        sa.Column("last_modified_at", sa.DateTime(), nullable=True),
        sa.Column("partition_columns", sa.JSON(), nullable=True),
        sa.Column("clustering_columns", sa.JSON(), nullable=True),
        sa.Column("properties", sa.JSON(), nullable=True),
        sa.Column("table_features", sa.JSON(), nullable=True),
        sa.Column("stats_error", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "full_name"),
        schema="inventory",
    )

    op.create_table(
        "cluster_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("policy_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("definition", sa.JSON(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "policy_id"),
        schema="inventory",
    )

    op.create_table(
        "compute_resources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("resource_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("resource_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("resource_name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("state", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("creator", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("driver_node_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("worker_node_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("num_workers", sa.Integer(), nullable=True),
        sa.Column("min_workers", sa.Integer(), nullable=True),
        sa.Column("max_workers", sa.Integer(), nullable=True),
        sa.Column("autoscale", sa.Boolean(), nullable=False),
        sa.Column("spot_enabled", sa.Boolean(), nullable=False),
        sa.Column("spot_fallback", sa.Boolean(), nullable=False),
        sa.Column("autotermination_minutes", sa.Integer(), nullable=True),
        sa.Column("cluster_source", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("warehouse_type", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("warehouse_size", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("spark_config", sa.JSON(), nullable=True),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("last_activity_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["connection_id"], ["core.provider_connections.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("connection_id", "resource_type", "resource_id"),
        schema="inventory",
    )
    op.create_index(
        "ix_compute_resources_resource_type",
        "compute_resources",
        ["resource_type"],
        schema="inventory",
    )
    op.create_index(
        "ix_compute_resources_state",
        "compute_resources",
        ["state"],
        schema="inventory",
    )

    op.create_table(
        "infra_cost_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("service", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("resource_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("platform_resource_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("usage_quantity", sa.Float(), nullable=False),
        sa.Column("usage_unit", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="inventory",
    )

    op.create_table(
        "infra_resource_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("provider", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("platform_resource_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("platform_resource_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("infra_resource_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("infra_resource_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("infra_resource_tags", sa.JSON(), nullable=True),
        sa.Column("hourly_cost_usd", sa.Float(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="inventory",
    )

    op.create_table(
        "s3_inventory_objects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("bucket", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("last_modified", sa.DateTime(), nullable=False),
        sa.Column("storage_class", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("etag", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("matched_table", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("matched_table_location", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("is_orphan", sa.Boolean(), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("inventory_run_id", sa.Uuid(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        schema="inventory",
    )
    op.create_index(
        "ix_s3_inventory_objects_bucket_key",
        "s3_inventory_objects",
        ["bucket", "key"],
        schema="inventory",
    )
    op.create_index(
        "ix_s3_inventory_objects_is_orphan",
        "s3_inventory_objects",
        ["is_orphan"],
        schema="inventory",
    )


def downgrade() -> None:
    # inventory tables
    op.drop_index(
        "ix_s3_inventory_objects_is_orphan",
        table_name="s3_inventory_objects",
        schema="inventory",
    )
    op.drop_index(
        "ix_s3_inventory_objects_bucket_key",
        table_name="s3_inventory_objects",
        schema="inventory",
    )
    op.drop_table("s3_inventory_objects", schema="inventory")
    op.drop_table("infra_resource_mappings", schema="inventory")
    op.drop_index(
        "ix_compute_resources_state",
        table_name="compute_resources",
        schema="inventory",
    )
    op.drop_index(
        "ix_compute_resources_resource_type",
        table_name="compute_resources",
        schema="inventory",
    )
    op.drop_table("compute_resources", schema="inventory")
    op.drop_table("infra_cost_snapshots", schema="inventory")
    op.drop_table("cluster_policies", schema="inventory")
    op.drop_table("unity_catalog_tables", schema="inventory")
    op.drop_table("query_plans", schema="inventory")
    op.drop_table("query_history", schema="inventory")
    op.drop_table("job_runs", schema="inventory")
    op.drop_table("job_profiles", schema="inventory")
    op.drop_table("billing_records", schema="inventory")

    # core tables
    op.drop_table("worker_cursors", schema="core")
    op.drop_table("collection_runs", schema="core")
    op.drop_table("audit_log", schema="core")
    op.drop_table("recommendations", schema="core")
    op.drop_table("consolidation_groups", schema="core")
    op.drop_table("analysis_runs", schema="core")
    op.drop_table("api_keys", schema="core")
    op.drop_table("provider_connections", schema="core")
