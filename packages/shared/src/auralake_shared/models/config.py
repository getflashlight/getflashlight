from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class AutomationLevel(StrEnum):
    RECOMMEND = "recommend"
    DRY_RUN = "dry_run"
    APPLY = "apply"
    AUTO = "auto"


class DatabaseConfig(BaseModel):
    url: str = "postgresql://auralake:password@localhost:5432/auralake"


class DefaultsConfig(BaseModel):
    automation_level: AutomationLevel = AutomationLevel.RECOMMEND
    output_format: str = "table"
    lookback_days: int = 30


class GitHubConfig(BaseModel):
    repo: str = ""
    token_env: str = "GITHUB_TOKEN"
    local_path: str | None = None
    base_branch: str = "main"
    pr_labels: list[str] = Field(default_factory=lambda: ["auralake", "cost-optimization"])
    bundle_paths: dict[str, str] = Field(default_factory=dict)


class DatabricksWorkspaceConfig(BaseModel):
    host: str
    is_default: bool = False
    sql_warehouse_id: str | None = None
    bundle_path: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)


class AWSCostExplorerConfig(BaseModel):
    enabled: bool = True
    cluster_tag_key: str = "ClusterId"
    tag_filters: dict[str, str] = Field(default_factory=lambda: {"Vendor": "Databricks"})


class DatabricksAWSConfig(BaseModel):
    region: str = "us-east-1"
    cost_explorer: AWSCostExplorerConfig = Field(default_factory=AWSCostExplorerConfig)


class DatabricksAccountConfig(BaseModel):
    host: str = ""
    account_id: str = ""


class DatabricksConfig(BaseModel):
    account: DatabricksAccountConfig = Field(default_factory=DatabricksAccountConfig)
    workspaces: dict[str, DatabricksWorkspaceConfig] = Field(default_factory=dict)
    aws: DatabricksAWSConfig = Field(default_factory=DatabricksAWSConfig)


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
    tag_policy: TagPolicyConfig = Field(default_factory=TagPolicyConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
