from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class JobProfile(BaseModel):
    job_id: str
    job_name: str
    workspace_id: str | None = None
    schedule_cron: str | None = None
    avg_duration_minutes: float = 0.0
    avg_dbu_cost: float = 0.0
    instance_type: str | None = None
    worker_count: int = 0
    spark_config: dict[str, Any] = Field(default_factory=dict)
    data_sources: list[str] = Field(default_factory=list)
    databricks_features_used: list[str] = Field(default_factory=list)
    dab_file_path: str | None = None
    dab_job_key: str | None = None
    is_portable: bool = True
    consolidation_group_id: str | None = None


class ConsolidationGroup(BaseModel):
    group_name: str
    workspace_id: str | None = None
    job_ids: list[str] = Field(default_factory=list)
    recommended_cluster_config: dict[str, Any] = Field(default_factory=dict)
    recommended_dab_changes: list[dict[str, Any]] = Field(default_factory=list)
    estimated_monthly_savings_usd: float = 0.0
    status: str = "proposed"
    pr_url: str | None = None
