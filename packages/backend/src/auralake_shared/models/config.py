from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class AutomationLevel(StrEnum):
    RECOMMEND = "recommend"
    DRY_RUN = "dry_run"
    APPLY = "apply"
    AUTO = "auto"


class ConnectionProvider(StrEnum):
    DATABRICKS = "databricks"
    SNOWFLAKE = "snowflake"
    LAKE_FORMATION = "lake_formation"
    GITHUB = "github"
    AWS = "aws"
    CONFIG = "config"


class BudgetScope(StrEnum):
    WORKSPACE = "workspace"
    ACCOUNT = "account"
    CLUSTER = "cluster"
    JOB = "job"


class DatabaseConfig(BaseModel):
    url: str = "postgresql+psycopg://localhost:5432/auralake"


class DefaultsConfig(BaseModel):
    automation_level: AutomationLevel = AutomationLevel.RECOMMEND
    output_format: str = "table"
    lookback_days: int = 30


class GitHubConfig(BaseModel):
    repo: str = ""
    token_env: str = "GITHUB_TOKEN"
    token: str | None = Field(default=None, exclude=True)
    local_path: str | None = None
    base_branch: str = "main"
    pr_labels: list[str] = Field(default_factory=lambda: ["auralake", "cost-optimization"])
    bundle_paths: dict[str, str] = Field(default_factory=dict)


class DatabricksWorkspaceConfig(BaseModel):
    host: str
    is_default: bool = False
    token: str | None = Field(default=None, exclude=True)
    client_id: str | None = Field(default=None, exclude=True)
    client_secret: str | None = Field(default=None, exclude=True)
    sql_warehouse_id: str | None = None
    bundle_path: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class AWSCostExplorerConfig(BaseModel):
    enabled: bool = True
    cluster_tag_key: str = "ClusterId"
    tag_filters: dict[str, str] = Field(default_factory=lambda: {"Vendor": "Databricks"})


class S3InventoryConfig(BaseModel):
    enabled: bool = False
    destination_bucket: str = ""
    destination_prefix: str = "auralake-inventory"
    frequency: str = "Daily"  # "Daily" or "Weekly"
    included_fields: list[str] = Field(
        default_factory=lambda: [
            "Size",
            "LastModifiedDate",
            "StorageClass",
            "ETag",
            "IsMultipartUploaded",
            "ObjectLockRetainUntilDate",
        ]
    )


class DatabricksAWSConfig(BaseModel):
    region: str = "us-east-1"
    access_key_id: str | None = Field(default=None, exclude=True)
    secret_access_key: str | None = Field(default=None, exclude=True)
    session_token: str | None = Field(default=None, exclude=True)
    cost_explorer: AWSCostExplorerConfig = Field(default_factory=AWSCostExplorerConfig)
    s3_inventory: S3InventoryConfig = Field(default_factory=S3InventoryConfig)


class DatabricksAccountConfig(BaseModel):
    host: str = ""
    account_id: str = ""


class DatabricksDiscountConfig(BaseModel):
    """Company-negotiated Databricks pricing."""

    global_dbu_discount_pct: float = 0.0  # e.g. 0.25 = 25% off list
    sku_overrides: dict[str, float] = Field(default_factory=dict)  # sku → negotiated $/DBU


class AWSDiscountConfig(BaseModel):
    """Company-negotiated AWS pricing."""

    edp_discount_pct: float = 0.0  # e.g. 0.10 = 10% EDP discount
    has_reserved_instances: bool = False
    has_savings_plans: bool = False


class DiscountConfig(BaseModel):
    databricks: DatabricksDiscountConfig = Field(default_factory=DatabricksDiscountConfig)
    aws: AWSDiscountConfig = Field(default_factory=AWSDiscountConfig)


class DatabricksConfig(BaseModel):
    account: DatabricksAccountConfig = Field(default_factory=DatabricksAccountConfig)
    workspaces: dict[str, DatabricksWorkspaceConfig] = Field(default_factory=dict)
    aws: DatabricksAWSConfig = Field(default_factory=DatabricksAWSConfig)
    discounts: DiscountConfig = Field(default_factory=DiscountConfig)


class ClusterRightsizingThresholds(BaseModel):
    cpu_utilization_low: int = 20
    cpu_utilization_target: int = 60
    memory_utilization_low: int = 30
    min_savings_dollars: int = 50


class IdleResourceThresholds(BaseModel):
    idle_cluster_minutes: int = 60
    stale_job_days: int = 90


class SpotThresholds(BaseModel):
    min_savings_pct: int = 30


class DeltaMaintenanceThresholds(BaseModel):
    optimize_threshold_gb: int = 1
    vacuum_retention_hours: int = 168
    small_file_threshold_mb: int = 32
    optimize_stale_days: int = 7
    vacuum_stale_days: int = 14
    over_optimize_threshold: int = 3
    min_table_size_gb_for_history: float = 0.1


class JobConsolidationThresholds(BaseModel):
    min_group_size: int = 2
    min_savings_dollars: int = 100
    max_group_size: int = 10


class ThresholdsConfig(BaseModel):
    cluster_rightsizing: ClusterRightsizingThresholds = Field(
        default_factory=ClusterRightsizingThresholds
    )
    idle_resources: IdleResourceThresholds = Field(default_factory=IdleResourceThresholds)
    spot_optimization: SpotThresholds = Field(default_factory=SpotThresholds)
    delta_maintenance: DeltaMaintenanceThresholds = Field(
        default_factory=DeltaMaintenanceThresholds
    )
    job_consolidation: JobConsolidationThresholds = Field(
        default_factory=JobConsolidationThresholds
    )


class RequiredTag(BaseModel):
    key: str


class TagPolicyConfig(BaseModel):
    required_tags: list[RequiredTag] = Field(default_factory=list)


class RuleConfig(BaseModel):
    enabled: bool = True
    thresholds: dict[str, float | int | str] = Field(default_factory=dict)


class RulesConfig(BaseModel):
    # Cluster rules
    cluster_rightsize: RuleConfig = Field(default_factory=RuleConfig)
    cluster_idle: RuleConfig = Field(default_factory=RuleConfig)
    cluster_no_autotermination: RuleConfig = Field(default_factory=RuleConfig)
    cluster_spot_eligible: RuleConfig = Field(default_factory=RuleConfig)
    # Cost rules
    cost_high_sku: RuleConfig = Field(default_factory=RuleConfig)
    # Delta rules
    delta_small_files: RuleConfig = Field(default_factory=RuleConfig)
    delta_stale_optimize: RuleConfig = Field(default_factory=RuleConfig)
    delta_stale_vacuum: RuleConfig = Field(default_factory=RuleConfig)
    delta_over_optimized: RuleConfig = Field(default_factory=RuleConfig)
    delta_migrate_to_liquid_clustering: RuleConfig = Field(default_factory=RuleConfig)
    delta_enable_clustering: RuleConfig = Field(default_factory=RuleConfig)
    # Job rules
    job_stale: RuleConfig = Field(default_factory=RuleConfig)
    job_failing: RuleConfig = Field(default_factory=RuleConfig)
    job_consolidation: RuleConfig = Field(default_factory=RuleConfig)
    # Query rules
    query_expensive: RuleConfig = Field(default_factory=RuleConfig)
    query_anti_pattern: RuleConfig = Field(default_factory=RuleConfig)
    # Infra rules
    infra_high_transfer: RuleConfig = Field(default_factory=RuleConfig)
    # Spot rules
    spot_eligible: RuleConfig = Field(default_factory=RuleConfig)
    # Idle rules
    idle_cluster: RuleConfig = Field(default_factory=RuleConfig)
    # S3 rules
    orphan_s3_objects: RuleConfig = Field(default_factory=RuleConfig)
    untagged_s3_objects: RuleConfig = Field(default_factory=RuleConfig)


class AutomationConfig(BaseModel):
    max_auto_risk_level: str = "medium"
    bulk_action_threshold: int = 5
    protected_clusters: list[str] = Field(default_factory=list)
    protected_jobs: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    interval_seconds: int = 300
    query_lookback_hours: int = 24
    max_queries_per_run: int = 1000


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    api_key_env: str = "AURALAKE_API_KEY"
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])


class AuraLakeConfig(BaseModel):
    version: str = "1"
    provider: str = "databricks"
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    databricks: DatabricksConfig = Field(default_factory=DatabricksConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    thresholds: ThresholdsConfig = Field(default_factory=ThresholdsConfig)
    rules: RulesConfig = Field(default_factory=RulesConfig)
    tag_policy: TagPolicyConfig = Field(default_factory=TagPolicyConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
