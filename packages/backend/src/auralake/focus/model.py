"""The canonical internal FOCUS record produced by every connector.

Each source connector maps its native billing export into a ``FocusRecord``.
This is the single contract between ingestion and the store: connectors differ,
``FocusRecord`` does not. Persistence (the BRONZE table) mirrors these fields.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from auralake.focus.enums import (
    ChargeCategory,
    ChargeClass,
    ComputeClass,
    ServiceCategory,
)

# Internal canonical FOCUS version we normalize every source into.
FOCUS_VERSION = "1.1"


class FocusRecord(BaseModel):
    """One normalized charge line in canonical FOCUS form.

    Field names use snake_case internally; the FOCUS column name is noted in the
    comment where it differs. The five cost columns are kept distinct on purpose
    — downstream views pick exactly one (see :class:`~auralake.focus.enums.CostMetric`).
    """

    # ── Provenance / accounts ──────────────────────────────────────────────
    # ProviderName is an OPEN dimension in FOCUS (AWS, Microsoft, Oracle, Google,
    # Databricks, …) — kept as a free string, not a closed enum. See
    # auralake.focus.enums.ProviderName for the constants our connectors emit.
    provider_name: str  # FOCUS: ProviderName
    billing_account_id: str  # FOCUS: BillingAccountId
    billing_account_name: str | None = None
    sub_account_id: str | None = None  # FOCUS: SubAccountId (e.g. workspace, project)
    sub_account_name: str | None = None

    # ── Periods (charge period is the additive grain; billing period is not) ─
    billing_period_start: date  # FOCUS: BillingPeriodStart
    billing_period_end: date  # FOCUS: BillingPeriodEnd
    charge_period_start: datetime  # FOCUS: ChargePeriodStart
    charge_period_end: datetime  # FOCUS: ChargePeriodEnd

    # ── Costs (one currency per record; never sum across currencies) ────────
    billing_currency: str = "USD"  # FOCUS: BillingCurrency
    billed_cost: Decimal = Decimal("0")  # FOCUS: BilledCost
    effective_cost: Decimal = Decimal("0")  # FOCUS: EffectiveCost (default for TCO)
    list_cost: Decimal = Decimal("0")  # FOCUS: ListCost
    contracted_cost: Decimal = Decimal("0")  # FOCUS: ContractedCost

    # ── Charge classification ───────────────────────────────────────────────
    charge_category: ChargeCategory  # FOCUS: ChargeCategory
    charge_class: ChargeClass | None = None  # FOCUS: ChargeClass (Correction | null)
    charge_description: str | None = None  # FOCUS: ChargeDescription

    # ── Service / SKU / location ────────────────────────────────────────────
    service_category: ServiceCategory  # FOCUS: ServiceCategory
    service_name: str  # FOCUS: ServiceName
    sku_id: str | None = None  # FOCUS: SkuId
    region_id: str | None = None  # FOCUS: RegionId

    # ── Resource ────────────────────────────────────────────────────────────
    resource_id: str | None = None  # FOCUS: ResourceId
    resource_name: str | None = None  # FOCUS: ResourceName
    resource_type: str | None = None  # FOCUS: ResourceType

    # ── Usage ───────────────────────────────────────────────────────────────
    consumed_quantity: float | None = None  # FOCUS: ConsumedQuantity
    consumed_unit: str | None = None  # FOCUS: ConsumedUnit

    # ── Tags (used for TCO attribution) ─────────────────────────────────────
    tags: dict[str, str] = Field(default_factory=dict)  # FOCUS: Tags

    # ── Auralake extensions (x_ prefix mirrors FOCUS provider-extension style)
    x_compute_class: ComputeClass = ComputeClass.NOT_APPLICABLE
    x_focus_version: str = FOCUS_VERSION
    x_source_connector: str = "unknown"

    @field_validator("billing_currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        return v.upper()

    def dedupe_key(self) -> str:
        """Stable natural key for idempotent upserts.

        Built from the dimensions that identify a unique charge line. Nullable
        parts are coalesced to ``""`` so Postgres unique semantics behave (NULLs
        never compare equal, which would let duplicates slip through).
        """
        parts = [
            str(self.provider_name),
            self.billing_account_id,
            self.sub_account_id or "",
            self.charge_period_start.isoformat(),
            self.charge_period_end.isoformat(),
            self.service_name,
            self.sku_id or "",
            self.resource_id or "",
            self.charge_category.value,
            self.charge_class.value if self.charge_class else "",
            self.x_source_connector,
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()
