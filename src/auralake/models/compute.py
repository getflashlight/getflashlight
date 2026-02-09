from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ClusterInfo(BaseModel):
    cluster_id: str
    cluster_name: str
    state: str  # RUNNING, TERMINATED, PENDING, etc.
    driver_node_type: str | None = None
    worker_node_type: str | None = None
    num_workers: int = 0
    min_workers: int | None = None
    max_workers: int | None = None
    autoscale: bool = False
    spot_enabled: bool = False
    spot_fallback: bool = False
    autotermination_minutes: int | None = None
    cluster_source: str | None = None  # "UI", "JOB", "API"
    creator: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    started_at: datetime | None = None
    last_activity_at: datetime | None = None
    workspace_id: str | None = None


class ClusterUtilization(BaseModel):
    cluster_id: str
    period_days: int
    avg_cpu_percent: float = 0.0
    max_cpu_percent: float = 0.0
    avg_memory_percent: float = 0.0
    max_memory_percent: float = 0.0
    avg_worker_count: float = 0.0
    max_worker_count: float = 0.0
    active_hours: float = 0.0
    idle_hours: float = 0.0
    total_dbu: float = 0.0
    total_cost_usd: float = 0.0
