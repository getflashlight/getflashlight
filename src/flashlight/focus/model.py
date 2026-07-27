"""The canonical internal FOCUS record produced by every connector.

Each source connector maps its native billing export into a ``FocusRecord``.
This is the single contract between ingestion and the store: connectors differ,
``FocusRecord`` does not. Persistence (the BRONZE table) mirrors these fields.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from flashlight.focus.enums import (
    ChargeCategory,
    ChargeClass,
    ComputeClass,
    ServiceCategory,
)

# Internal canonical FOCUS version we normalize every source into.
FOCUS_VERSION = "1.1"


def _stringify_field(value: object) -> str:
    """One record field, as text for :meth:`FocusRecord.dedupe_key` — ``""`` for
    ``None`` (so an absent value can never collide with a real empty string),
    JSON for ``tags`` (deterministic key order), ISO-8601 for a date/datetime."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


class FocusRecord(BaseModel):
    """One normalized charge line in canonical FOCUS form.

    Field names use snake_case internally; the FOCUS column name is noted in the
    comment where it differs. The five cost columns are kept distinct on purpose
    — downstream views pick exactly one (see :class:`~flashlight.focus.enums.CostMetric`).
    """

    # ── Provenance / accounts ──────────────────────────────────────────────
    # ProviderName is an OPEN dimension in FOCUS (AWS, Microsoft, Oracle, Google,
    # Databricks, …) — kept as a free string, not a closed enum. See
    # flashlight.focus.enums.ProviderName for the constants our connectors emit.
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

    # ── Flashlight extensions (x_ prefix mirrors FOCUS provider-extension style)
    x_compute_class: ComputeClass = ComputeClass.NOT_APPLICABLE
    x_focus_version: str = FOCUS_VERSION
    x_source_connector: str = "unknown"
    # True when EffectiveCost could only be sourced from list/rack rates (no
    # negotiated account-price table available) — i.e. discounts are NOT reflected.
    # Surfaced rather than hidden so consumers know the cost may read high.
    x_effective_is_list: bool = False
    # Source-record identity. For Databricks these come from system.billing.usage's
    # append-only correction model: a correction emits a RETRACTION (same record_id,
    # negated quantity) + RESTATEMENT, leaving the ORIGINAL. Both are part of the
    # dedupe key so all three survive as distinct rows; aggregation SUMs them, and the
    # negative retraction nets out the original. (Empty for sources without this model.)
    x_record_id: str | None = None
    x_record_type: str | None = None  # ORIGINAL | RETRACTION | RESTATEMENT
    # Finer-grained-than-SKU cost bucket a connector can optionally stamp (e.g.
    # compute vs concurrency-scaling vs storage for a Redshift charge). Generic
    # so any connector can populate it, not just the one that introduced it.
    x_cost_subcategory: str | None = None

    @field_validator("billing_currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        return v.upper()

    def dedupe_key(self) -> str:
        """Stable natural key for collapsing a physically-duplicated row.

        Hashes every field on the record, not a curated subset of "identifying"
        dimensions — a curated subset silently conflates genuinely distinct
        charges that happen to share every dimension it tracks but differ in a
        field it doesn't. Confirmed against a real AWS export: multiple Reserved
        Instance purchases in the same period/SKU (AWS leaves ResourceId null on
        a purchase — it isn't tied to one resource) all shared the old key and
        collapsed to one, silently dropping the others. Two records now collapse
        only when they're identical in every field, which is the only case
        that's actually "the same physical row appeared twice."
        """
        parts = [
            _stringify_field(getattr(self, name))
            for name in sorted(type(self).model_fields)
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()
