"""The canonical internal efficiency record — the FOCUS-analog for *waste*.

Each connector that exposes utilization/activity telemetry maps its platform into an
``EfficiencyRecord``. This is the single contract between the efficiency pull and the
store, exactly as :class:`~auralake.focus.model.FocusRecord` is for cost. It is
deliberately small: the least standardized data that lets a customer see *waste +
cause + owner* across platforms.

Rows are **aggregated at source** to one row per (entity × month) — never per
execution. Distribution stats (run_count, pct_runs_underutilized, …) and any
platform-specific cause inputs live in ``cause_detail`` (a JSON map, the
``FocusRecord.tags`` pattern), keeping the standardized spine flat. The *waste
interpretation* (category + recoverable dollars) is derived downstream in GOLD SQL,
not on this record.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EntityType(StrEnum):
    """The actionable unit, platform-agnostic. A customer tunes/moves/bills one of these.

    ``job`` carries real per-run utilization; ``interactive``/``sql_warehouse`` are
    shared (cost-attributable per owner, but not per-user utilization); ``endpoint``
    waste is idle-provisioned, not utilization-based.
    """

    JOB = "job"
    INTERACTIVE = "interactive"
    SQL_WAREHOUSE = "sql_warehouse"
    ENDPOINT = "endpoint"


class EfficiencyRecord(BaseModel):
    """One entity's efficiency summary for one month, in canonical form."""

    # ── Identity / attribution ──────────────────────────────────────────────
    provider_name: str  # Databricks | BigQuery | Snowflake | … (partition key)
    charge_month: date  # first of month (partition key)
    entity_type: EntityType
    entity_id: str
    entity_name: str | None = None
    owner_user: str | None = None  # run_as (nullable for shared compute)
    owner_project: str | None = None  # cost-allocation tag (nullable)

    # ── The billed-vs-used signal ───────────────────────────────────────────
    billed_cost: Decimal = Decimal("0")  # reconciles to FOCUS
    native_quantity: float | None = None  # DBUs | slot-hrs | credits
    native_unit: str | None = None
    # 0–100, or None when not measurable (shared clusters/warehouses have no
    # per-entity CPU%). Underutilization waste is only ever claimed when this is set.
    utilization_pct: float | None = None
    activity_count: int | None = None  # runs | queries | requests (idle + confidence)

    # ── Cause specifics (the FocusRecord.tags pattern) ──────────────────────
    # e.g. run_count, pct_runs_underutilized, failed_cost, photon, provisioned_units.
    cause_detail: dict[str, Any] = Field(default_factory=dict)

    # ── Provenance ──────────────────────────────────────────────────────────
    x_source_connector: str = "unknown"

    @field_validator("charge_month")
    @classmethod
    def _first_of_month(cls, v: date) -> date:
        return v.replace(day=1)

    @field_validator("utilization_pct")
    @classmethod
    def _bounded(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 100.0):
            raise ValueError(f"utilization_pct must be 0–100, got {v}")
        return v
