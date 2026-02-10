"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-02-09

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    )

    op.create_table(
        "agent_state",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("last_query_timestamp", sa.DateTime(), nullable=True),
        sa.Column("last_job_run_timestamp", sa.DateTime(), nullable=True),
        sa.Column("queries_collected", sa.Integer(), nullable=False),
        sa.Column("plans_collected", sa.Integer(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
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
        sa.ForeignKeyConstraint(["analysis_run_id"], ["analysis_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
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
        sa.ForeignKeyConstraint(["recommendation_id"], ["recommendations.id"]),
        sa.PrimaryKeyConstraint("id"),
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
        sa.ForeignKeyConstraint(["consolidation_group_id"], ["consolidation_groups.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("job_profiles")
    op.drop_table("audit_log")
    op.drop_table("recommendations")
    op.drop_table("query_plans")
    op.drop_table("infra_resource_mappings")
    op.drop_table("infra_cost_snapshots")
    op.drop_table("agent_state")
    op.drop_table("consolidation_groups")
    op.drop_table("analysis_runs")
