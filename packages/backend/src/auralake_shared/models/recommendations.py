from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RecommendationStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    PR_CREATED = "pr_created"


class SavingsConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Recommendation(BaseModel):
    type: str
    risk_level: RiskLevel
    resource_id: str
    resource_name: str
    workspace_id: str | None = None
    title: str
    description: str
    current_state: dict[str, Any] = Field(default_factory=dict)
    recommended_state: dict[str, Any] = Field(default_factory=dict)
    estimated_monthly_savings_usd: Decimal = Decimal("0")
    savings_confidence: SavingsConfidence = SavingsConfidence.MEDIUM
    evidence: dict[str, Any] = Field(default_factory=dict)
    status: RecommendationStatus = RecommendationStatus.PENDING
    pricing_basis: str = "list"  # "list" or "negotiated"
    baseline_monthly_cost_usd: Decimal | None = None
    actual_monthly_savings_usd: Decimal | None = None
    savings_verified_at: datetime | None = None
    pr_url: str | None = None


class AnalysisResult(BaseModel):
    analyzer_name: str
    workspace_id: str | None = None
    provider: str = "databricks"
    recommendations: list[Recommendation] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ActionResult(BaseModel):
    action_type: str
    resource_id: str
    resource_name: str
    status: str  # "applied" | "failed" | "skipped" | "dry_run"
    detail: str | None = None
    error: str | None = None
    pr_url: str | None = None


class SavingsEstimate(BaseModel):
    category: str
    current_monthly_cost: Decimal
    projected_monthly_cost: Decimal
    estimated_monthly_savings: Decimal
    confidence: SavingsConfidence = SavingsConfidence.MEDIUM
