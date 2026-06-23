"""SQLModel tables for the BRONZE layer and ingestion metadata.

Only raw landing tables and run metadata are modeled here. The SILVER and GOLD
layers are SQL *views* created by the transform runner — not ORM tables — so the
metrics contract lives in version-controlled SQL, not Python.

Schemas:
  meta  — ingestion run bookkeeping
  raw   — BRONZE canonical FOCUS landing table
  silver/gold — views (see transform/sql/*.sql), not defined here
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, Numeric, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class IngestRun(SQLModel, table=True):
    """One execution of a connector ingest. Stamped onto every landed row."""

    __tablename__ = "ingest_run"
    __table_args__ = {"schema": "meta"}

    id: int | None = Field(default=None, primary_key=True)
    connector: str = Field(index=True)
    status: str = "running"  # running | success | failed
    started_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True))
    )
    finished_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True))
    )
    rows_ingested: int = 0
    detail: str | None = None


class RawFocusRecord(SQLModel, table=True):
    """BRONZE — one canonical FOCUS charge line. Nothing consumes this directly.

    ``dedupe_key`` carries a UNIQUE constraint so re-ingesting a restated export
    corrects the existing row (ON CONFLICT DO UPDATE) instead of duplicating it.
    """

    __tablename__ = "focus_record"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_focus_dedupe_key"),
        {"schema": "raw"},
    )

    id: int | None = Field(default=None, primary_key=True)
    dedupe_key: str = Field(index=True)
    ingest_run_id: int | None = Field(default=None, foreign_key="meta.ingest_run.id")

    # Provenance / accounts
    provider_name: str = Field(index=True)
    billing_account_id: str = Field(index=True)
    billing_account_name: str | None = None
    sub_account_id: str | None = Field(default=None, index=True)
    sub_account_name: str | None = None

    # Periods
    billing_period_start: date = Field(index=True)
    billing_period_end: date
    charge_period_start: datetime = Field(
        sa_column=Column(DateTime(timezone=True), index=True)
    )
    charge_period_end: datetime = Field(sa_column=Column(DateTime(timezone=True)))

    # Costs
    billing_currency: str = "USD"
    billed_cost: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(20, 6)))
    effective_cost: Decimal = Field(
        default=Decimal("0"), sa_column=Column(Numeric(20, 6))
    )
    list_cost: Decimal = Field(default=Decimal("0"), sa_column=Column(Numeric(20, 6)))
    contracted_cost: Decimal = Field(
        default=Decimal("0"), sa_column=Column(Numeric(20, 6))
    )

    # Classification
    charge_category: str = Field(index=True)
    charge_class: str | None = None
    charge_description: str | None = None

    # Service / SKU / location
    service_category: str = Field(index=True)
    service_name: str = Field(index=True)
    sku_id: str | None = None
    region_id: str | None = None

    # Resource
    resource_id: str | None = Field(default=None, index=True)
    resource_name: str | None = None
    resource_type: str | None = None

    # Usage
    consumed_quantity: float | None = None
    consumed_unit: str | None = None

    # Tags + extensions
    tags: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSONB))
    x_compute_class: str = "n/a"
    x_focus_version: str = "1.1"
    x_source_connector: str = "unknown"
    x_effective_is_list: bool = False
    x_record_id: str | None = None
    x_record_type: str | None = None
