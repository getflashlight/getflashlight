"""FOCUS controlled-vocabulary enums.

Values follow the FOCUS spec column library (https://focus.finops.org/focus-columns/).
We keep the spec-allowed values plus an explicit ``OTHER`` escape hatch so a
connector never has to invent an out-of-vocabulary value.
"""

from __future__ import annotations

from enum import StrEnum


class ProviderName(StrEnum):
    """Origin of the billing data (extends FOCUS ProviderName for our sources)."""

    AWS = "AWS"
    GCP = "GCP"
    AZURE = "Azure"
    DATABRICKS = "Databricks"
    SNOWFLAKE = "Snowflake"


class ChargeCategory(StrEnum):
    """FOCUS ChargeCategory — the top-level nature of a charge."""

    USAGE = "Usage"
    PURCHASE = "Purchase"
    TAX = "Tax"
    CREDIT = "Credit"
    ADJUSTMENT = "Adjustment"


class ChargeClass(StrEnum):
    """FOCUS ChargeClass — distinguishes corrections from original charges."""

    CORRECTION = "Correction"


class PricingCategory(StrEnum):
    """FOCUS PricingCategory — the pricing model behind a charge.

    ``DYNAMIC`` is the FOCUS spec's term for provider-variable pricing the customer
    can't lock in — on AWS that's exactly Spot (and other interruptible/low-priority
    pricing); there is no separate "spot" value in FOCUS, this is it. ``COMMITTED``
    means the charge itself is discounted by an existing commitment (a Reserved
    Instance/Savings Plan covering it) — distinct from ``STANDARD``, which is the
    on-demand/negotiated-rate case with no such discount applied.
    """

    STANDARD = "Standard"
    DYNAMIC = "Dynamic"
    COMMITTED = "Committed"
    OTHER = "Other"


class ServiceCategory(StrEnum):
    """FOCUS ServiceCategory — the highest-level grouping of a service."""

    AI_AND_MACHINE_LEARNING = "AI and Machine Learning"
    ANALYTICS = "Analytics"
    COMPUTE = "Compute"
    DATABASES = "Databases"
    DEVELOPER_TOOLS = "Developer Tools"
    MANAGEMENT_AND_GOVERNANCE = "Management and Governance"
    NETWORKING = "Networking"
    SECURITY_IDENTITY_AND_COMPLIANCE = "Security, Identity, and Compliance"
    STORAGE = "Storage"
    OTHER = "Other"


class CostMetric(StrEnum):
    """The five FOCUS cost columns. Used to *declare* which metric a view uses.

    Critical rule: a single aggregation must use exactly one of these — never
    sum across two different cost metrics.
    """

    BILLED_COST = "BilledCost"
    EFFECTIVE_COST = "EffectiveCost"
    LIST_COST = "ListCost"
    CONTRACTED_COST = "ContractedCost"


class CommitmentDiscountCategory(StrEnum):
    """FOCUS CommitmentDiscountCategory — whether the commitment discounts spend or usage."""

    SPEND = "Spend"
    USAGE = "Usage"


class CommitmentDiscountStatus(StrEnum):
    """FOCUS CommitmentDiscountStatus — whether this charge used the commitment or not."""

    USED = "Used"
    UNUSED = "Unused"


class ComputeClass(StrEnum):
    """Flashlight extension — how a lakehouse charge is billed against cloud infra.

    ``CLASSIC`` compute (customer-managed VMs) is billed by the lakehouse vendor
    in DBUs *and* by the cloud in separate infra lines. ``SERVERLESS`` compute is
    billed all-in by the vendor, with no separate cloud infra line.

    Stamped on every Databricks record at ingest (see
    ``connectors/databricks.py::compute_class_for_sku``) and carried through
    ``silver.focus_normalized``. Descriptive only — no GOLD view keys off it today.
    """

    CLASSIC = "classic"
    SERVERLESS = "serverless"
    NOT_APPLICABLE = "n/a"
