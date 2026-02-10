from __future__ import annotations

from pydantic import BaseModel, Field


class DatabricksFeatureDependency(BaseModel):
    feature: str  # "delta_live_tables", "photon", "unity_catalog", "dbutils", etc.
    usage_count: int = 0
    files: list[str] = Field(default_factory=list)
    portable_alternative: str | None = None


class WorkloadProfile(BaseModel):
    job_id: str
    job_name: str
    is_portable: bool = True
    feature_dependencies: list[DatabricksFeatureDependency] = Field(default_factory=list)
    portability_score: float = 1.0  # 0.0 = fully locked in, 1.0 = fully portable
    notes: list[str] = Field(default_factory=list)
