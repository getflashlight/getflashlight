"""SQLModel ORM models for all auralake database tables."""

from __future__ import annotations

import uuid
from datetime import date, datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# QueryPlan — Spark query plans captured by collector
# ---------------------------------------------------------------------------
class QueryPlan(SQLModel, table=True):
    __tablename__ = "query_plans"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: str
    query_id: str
    job_id: str | None = Field(default=None)
    job_run_id: str | None = Field(default=None)
    cluster_id: str | None = Field(default=None)
    query_text: str | None = Field(default=None)
    logical_plan: str | None = Field(default=None)
    physical_plan: str | None = Field(default=None)
    parsed_plan: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    anti_patterns: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    duration_ms: int | None = Field(default=None)
    rows_scanned: int | None = Field(default=None)
    bytes_read: int | None = Field(default=None)
    shuffle_bytes: int | None = Field(default=None)
    spill_bytes: int | None = Field(default=None)
    captured_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# AnalysisRun — History of analysis executions
# ---------------------------------------------------------------------------
class AnalysisRun(SQLModel, table=True):
    __tablename__ = "analysis_runs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    analyzer_name: str
    workspace_id: str | None = Field(default=None)
    provider: str
    config_snapshot: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    summary: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    started_at: datetime
    completed_at: datetime | None = Field(default=None)
    status: str  # running / completed / failed


# ---------------------------------------------------------------------------
# RecommendationRecord — Generated recommendations
# ---------------------------------------------------------------------------
class RecommendationRecord(SQLModel, table=True):
    __tablename__ = "recommendations"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    analysis_run_id: uuid.UUID | None = Field(default=None, foreign_key="analysis_runs.id")
    type: str
    risk_level: str
    resource_id: str
    resource_name: str
    workspace_id: str | None = Field(default=None)
    title: str
    description: str
    current_state: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    recommended_state: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    estimated_monthly_savings_usd: float
    savings_confidence: str
    evidence: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    status: str  # pending / applied / dismissed / pr_created
    applied_at: datetime | None = Field(default=None)
    pr_url: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# AuditLog — All actions taken / attempted
# ---------------------------------------------------------------------------
class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    recommendation_id: uuid.UUID | None = Field(default=None, foreign_key="recommendations.id")
    action_type: str
    resource_id: str
    workspace_id: str | None = Field(default=None)
    provider: str
    automation_level: str
    before_state: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    after_state: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    status: str  # started / completed / failed / denied / skipped
    error_message: str | None = Field(default=None)
    pr_url: str | None = Field(default=None)
    executed_by: str | None = Field(default=None)
    executed_at: datetime


# ---------------------------------------------------------------------------
# JobProfileRecord — Cached job analysis
# ---------------------------------------------------------------------------
class JobProfileRecord(SQLModel, table=True):
    __tablename__ = "job_profiles"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: str | None = Field(default=None)
    job_id: str
    job_name: str
    schedule_cron: str | None = Field(default=None)
    avg_duration_minutes: float
    avg_dbu_cost: float
    instance_type: str | None = Field(default=None)
    worker_count: int
    spark_config: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    data_sources: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    databricks_features_used: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    dab_file_path: str | None = Field(default=None)
    dab_job_key: str | None = Field(default=None)
    is_portable: bool
    consolidation_group_id: uuid.UUID | None = Field(
        default=None, foreign_key="consolidation_groups.id"
    )
    last_analyzed_at: datetime


# ---------------------------------------------------------------------------
# ConsolidationGroupRecord — Job consolidation recommendations
# ---------------------------------------------------------------------------
class ConsolidationGroupRecord(SQLModel, table=True):
    __tablename__ = "consolidation_groups"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: str | None = Field(default=None)
    group_name: str
    recommended_cluster_config: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    recommended_dab_changes: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    estimated_monthly_savings_usd: float
    job_count: int
    status: str
    pr_url: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# InfraResourceMapping — Maps platform resources to infra resources
# ---------------------------------------------------------------------------
class InfraResourceMapping(SQLModel, table=True):
    __tablename__ = "infra_resource_mappings"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: str | None = Field(default=None)
    provider: str
    platform_resource_type: str
    platform_resource_id: str
    infra_resource_type: str
    infra_resource_id: str
    infra_resource_tags: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    hourly_cost_usd: float | None = Field(default=None)
    last_seen_at: datetime
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# InfraCostSnapshot — AWS cost snapshots
# ---------------------------------------------------------------------------
class InfraCostSnapshot(SQLModel, table=True):
    __tablename__ = "infra_cost_snapshots"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: str | None = Field(default=None)
    provider: str
    period_start: date
    period_end: date
    service: str
    resource_id: str
    platform_resource_id: str | None = Field(default=None)
    cost_usd: float
    usage_quantity: float
    usage_unit: str
    tags: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# AgentState — Collector bookkeeping
# ---------------------------------------------------------------------------
class AgentState(SQLModel, table=True):
    __tablename__ = "agent_state"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: str
    last_query_timestamp: datetime | None = Field(default=None)
    last_job_run_timestamp: datetime | None = Field(default=None)
    queries_collected: int
    plans_collected: int
    last_run_at: datetime | None = Field(default=None)
    status: str
