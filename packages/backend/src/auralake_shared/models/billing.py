from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class CostRecord(BaseModel):
    date: date
    workspace_id: str | None = None
    cluster_id: str | None = None
    job_id: str | None = None
    sku: str | None = None
    tag: str | None = None
    dbu_usage: float = 0.0
    cost_usd: Decimal = Decimal("0")


class PriceRecord(BaseModel):
    sku: str
    unit_price_usd: Decimal
    currency: str = "USD"
    effective_date: date | None = None


class CostBreakdown(BaseModel):
    total_cost_usd: Decimal
    by_sku: dict[str, Decimal] = Field(default_factory=dict)
    by_cluster: dict[str, Decimal] = Field(default_factory=dict)
    by_job: dict[str, Decimal] = Field(default_factory=dict)
    by_tag: dict[str, Decimal] = Field(default_factory=dict)
    period_start: date
    period_end: date


class InfraComputeCost(BaseModel):
    resource_id: str
    platform_resource_id: str | None = None
    service: str  # AmazonEC2, etc.
    instance_type: str | None = None
    cost_usd: Decimal
    usage_hours: float = 0.0
    period_start: date
    period_end: date


class InfraStorageCost(BaseModel):
    bucket_or_volume: str
    service: str  # AmazonS3, AmazonEBS
    cost_usd: Decimal
    storage_gb: float = 0.0
    api_calls: int = 0
    period_start: date
    period_end: date


class DataTransferCost(BaseModel):
    source: str
    destination: str
    cost_usd: Decimal
    transfer_gb: float = 0.0
    period_start: date
    period_end: date


class ResourceMapping(BaseModel):
    platform_resource_type: str
    platform_resource_id: str
    infra_resource_type: str
    infra_resource_id: str
    tags: dict[str, str] = Field(default_factory=dict)
    hourly_cost_usd: Decimal | None = None


class RISavingsPlanRec(BaseModel):
    resource_id: str
    current_cost_usd: Decimal
    recommended_commitment: str  # "1yr_ri", "3yr_ri", "1yr_savings_plan"
    estimated_savings_usd: Decimal
    breakeven_months: int
    confidence: float = 0.0


class TCORecord(BaseModel):
    """Total cost of ownership for a single resource."""

    resource_name: str
    resource_id: str
    dbu_cost: Decimal = Decimal("0")
    ec2_cost: Decimal = Decimal("0")
    ebs_cost: Decimal = Decimal("0")
    s3_cost: Decimal = Decimal("0")
    transfer_cost: Decimal = Decimal("0")

    @property
    def total_cost(self) -> Decimal:
        return self.dbu_cost + self.ec2_cost + self.ebs_cost + self.s3_cost + self.transfer_cost
