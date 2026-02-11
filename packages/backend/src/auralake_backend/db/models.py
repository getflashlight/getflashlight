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
    __table_args__ = {"schema": "inventory"}

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
    __table_args__ = {"schema": "core"}

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
    __table_args__ = {"schema": "core"}

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    analysis_run_id: uuid.UUID | None = Field(default=None, foreign_key="core.analysis_runs.id")
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
    __table_args__ = {"schema": "core"}

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    recommendation_id: uuid.UUID | None = Field(default=None, foreign_key="core.recommendations.id")
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
    __table_args__ = {"schema": "inventory"}

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
        default=None, foreign_key="core.consolidation_groups.id"
    )
    last_analyzed_at: datetime


# ---------------------------------------------------------------------------
# ConsolidationGroupRecord — Job consolidation recommendations
# ---------------------------------------------------------------------------
class ConsolidationGroupRecord(SQLModel, table=True):
    __tablename__ = "consolidation_groups"
    __table_args__ = {"schema": "core"}

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
    __table_args__ = {"schema": "inventory"}

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
# ComputeResourceRecord — Full compute config for clusters & warehouses
# ---------------------------------------------------------------------------
class ComputeResourceRecord(SQLModel, table=True):
    __tablename__ = "compute_resources"
    __table_args__ = (
        sa.UniqueConstraint("connection_id", "resource_type", "resource_id"),
        {"schema": "inventory"},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")
    workspace_id: str | None = Field(default=None)
    resource_type: str  # all_purpose_cluster / job_cluster / sql_warehouse
    resource_id: str  # cluster_id or warehouse_id
    resource_name: str
    state: str  # RUNNING, TERMINATED, STOPPED, etc.
    creator: str | None = Field(default=None)

    # Cluster fields
    driver_node_type: str | None = Field(default=None)
    worker_node_type: str | None = Field(default=None)
    num_workers: int | None = Field(default=None)
    min_workers: int | None = Field(default=None)
    max_workers: int | None = Field(default=None)
    autoscale: bool = Field(default=False)
    spot_enabled: bool = Field(default=False)
    spot_fallback: bool = Field(default=False)
    autotermination_minutes: int | None = Field(default=None)
    cluster_source: str | None = Field(default=None)  # UI / JOB / API

    # Warehouse fields
    warehouse_type: str | None = Field(default=None)  # PRO / CLASSIC / SERVERLESS
    warehouse_size: str | None = Field(default=None)  # 2X-Small, Small, Medium, etc.

    # Flexible storage
    tags: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    spark_config: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    config: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))

    started_at: datetime | None = Field(default=None)
    last_activity_at: datetime | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# InfraCostSnapshot — AWS cost snapshots
# ---------------------------------------------------------------------------
class InfraCostSnapshot(SQLModel, table=True):
    __tablename__ = "infra_cost_snapshots"
    __table_args__ = {"schema": "inventory"}

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
# ProviderConnection — Encrypted provider credentials
# ---------------------------------------------------------------------------
class ProviderConnection(SQLModel, table=True):
    __tablename__ = "provider_connections"
    __table_args__ = (
        sa.UniqueConstraint("provider", "name"),
        {"schema": "core"},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    provider: str  # "databricks", "github", "aws"
    name: str  # "production", "staging", "default"
    is_default: bool = Field(default=False)
    config: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    encrypted_credentials: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# S3InventoryObject — S3 inventory report objects with table mapping
# ---------------------------------------------------------------------------
class S3InventoryObject(SQLModel, table=True):
    __tablename__ = "s3_inventory_objects"
    __table_args__ = {"schema": "inventory"}

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    bucket: str
    key: str
    size_bytes: int
    last_modified: datetime
    storage_class: str | None = Field(default=None)
    etag: str | None = Field(default=None)
    matched_table: str | None = Field(default=None)
    matched_table_location: str | None = Field(default=None)
    is_orphan: bool = Field(default=False)
    tags: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    inventory_run_id: uuid.UUID | None = Field(default=None)
    collected_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# ApiKey — SHA-256 hashed API keys
# ---------------------------------------------------------------------------
class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"
    __table_args__ = {"schema": "core"}

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str
    key_hash: str
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_used_at: datetime | None = Field(default=None)


# ---------------------------------------------------------------------------
# CollectionRun — Track each collection execution
# ---------------------------------------------------------------------------
class CollectionRun(SQLModel, table=True):
    __tablename__ = "collection_runs"
    __table_args__ = {"schema": "core"}

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")
    status: str  # pending / running / completed / completed_with_errors / failed / cancelled
    trigger: str  # manual / auto / scheduled
    worker_statuses: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    error: str | None = Field(default=None)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = Field(default=None)


# ---------------------------------------------------------------------------
# WorkerCursor — Per-worker watermarks for incremental collection
# ---------------------------------------------------------------------------
class WorkerCursor(SQLModel, table=True):
    __tablename__ = "worker_cursors"
    __table_args__ = (
        sa.UniqueConstraint("connection_id", "worker_name"),
        {"schema": "core"},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")
    worker_name: str
    cursor_value: str
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# JobRunRecord — Job run history
# ---------------------------------------------------------------------------
class JobRunRecord(SQLModel, table=True):
    __tablename__ = "job_runs"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "run_id"),
        {"schema": "inventory"},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")
    workspace_id: str | None = Field(default=None)
    job_id: str
    run_id: str
    state: str  # SUCCESS / FAILED / TIMEDOUT / CANCELLED / etc.
    start_time: datetime | None = Field(default=None)
    end_time: datetime | None = Field(default=None)
    duration_ms: int | None = Field(default=None)
    cluster_id: str | None = Field(default=None)
    trigger: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# BillingRecord — DBU billing data
# ---------------------------------------------------------------------------
class BillingRecord(SQLModel, table=True):
    __tablename__ = "billing_records"
    __table_args__ = {"schema": "inventory"}

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")
    usage_date: date
    sku: str
    cluster_id: str | None = Field(default=None)
    job_id: str | None = Field(default=None)
    warehouse_id: str | None = Field(default=None)
    endpoint_id: str | None = Field(default=None)
    pipeline_id: str | None = Field(default=None)
    notebook_id: str | None = Field(default=None)
    workspace_id: str | None = Field(default=None)
    dbu_usage: float
    cost_usd: float
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# QueryHistoryRecord — Query history records
# ---------------------------------------------------------------------------
class QueryHistoryRecord(SQLModel, table=True):
    __tablename__ = "query_history"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "query_id"),
        {"schema": "inventory"},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")
    workspace_id: str | None = Field(default=None)
    query_id: str
    query_text: str | None = Field(default=None)
    status: str | None = Field(default=None)
    user_name: str | None = Field(default=None)
    warehouse_id: str | None = Field(default=None)
    duration_ms: int | None = Field(default=None)
    rows_produced: int | None = Field(default=None)
    query_start_time_ms: int | None = Field(
        default=None, sa_column=sa.Column(sa.BigInteger, nullable=True)
    )
    query_end_time_ms: int | None = Field(
        default=None, sa_column=sa.Column(sa.BigInteger, nullable=True)
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# ClusterPolicyRecord — Cluster policy definitions
# ---------------------------------------------------------------------------
class ClusterPolicyRecord(SQLModel, table=True):
    __tablename__ = "cluster_policies"
    __table_args__ = (
        sa.UniqueConstraint("connection_id", "policy_id"),
        {"schema": "inventory"},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")
    policy_id: str
    name: str
    description: str | None = Field(default=None)
    definition: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# UnityCatalogTableRecord — Unity Catalog table metadata
# ---------------------------------------------------------------------------
class UnityCatalogTableRecord(SQLModel, table=True):
    __tablename__ = "unity_catalog_tables"
    __table_args__ = (
        sa.UniqueConstraint("connection_id", "full_name"),
        {"schema": "inventory"},
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    connection_id: uuid.UUID = Field(foreign_key="core.provider_connections.id")

    # Identity
    catalog_name: str
    schema_name: str
    table_name: str
    full_name: str  # catalog.schema.table
    table_type: str  # MANAGED / EXTERNAL
    data_format: str | None = Field(default=None)  # delta, iceberg, etc.

    # Storage location (S3 path)
    location: str | None = Field(default=None)  # s3://bucket/path

    # DESCRIBE DETAIL metrics (for optimization decisions)
    size_bytes: int | None = Field(default=None, sa_column=sa.Column(sa.BigInteger, nullable=True))
    num_files: int | None = Field(default=None)
    owner: str | None = Field(default=None)
    last_modified_at: datetime | None = Field(default=None)
    partition_columns: list = Field(default_factory=list, sa_column=sa.Column(sa.JSON))
    clustering_columns: list = Field(default_factory=list, sa_column=sa.Column(sa.JSON))
    properties: dict = Field(default_factory=dict, sa_column=sa.Column(sa.JSON))
    table_features: list = Field(default_factory=list, sa_column=sa.Column(sa.JSON))

    # DESCRIBE DETAIL error (NULL = success or not yet attempted)
    stats_error: str | None = Field(default=None)

    # Maintenance history (from DESCRIBE HISTORY)
    last_optimized_at: datetime | None = Field(default=None)
    last_vacuumed_at: datetime | None = Field(default=None)
    optimize_count_30d: int | None = Field(default=None)
    vacuum_count_30d: int | None = Field(default=None)
    last_optimize_removed_files: int | None = Field(default=None)
    last_optimize_added_bytes: int | None = Field(
        default=None, sa_column=sa.Column(sa.BigInteger, nullable=True)
    )
    uses_liquid_clustering: bool = Field(default=False)
    uses_zordering: bool = Field(default=False)
    history_error: str | None = Field(default=None)

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen_at: datetime = Field(default_factory=datetime.utcnow)
