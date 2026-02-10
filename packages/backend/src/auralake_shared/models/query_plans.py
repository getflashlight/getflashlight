from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PlanNode(BaseModel):
    node_type: str
    name: str
    children: list[PlanNode] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    rows_estimate: int | None = None
    size_estimate: int | None = None


class PlanAntiPattern(BaseModel):
    type: str  # "full_scan", "bad_join", "no_pruning", "excessive_shuffle"
    description: str
    severity: str  # "low", "medium", "high"
    node_path: str | None = None
    recommendation: str | None = None


class SparkPlan(BaseModel):
    query_id: str
    query_text: str | None = None
    logical_plan: str | None = None
    physical_plan: str | None = None
    parsed_nodes: list[PlanNode] = Field(default_factory=list)
    anti_patterns: list[PlanAntiPattern] = Field(default_factory=list)
    duration_ms: int | None = None
    rows_scanned: int | None = None
    bytes_read: int | None = None
    shuffle_bytes: int | None = None
    spill_bytes: int | None = None
