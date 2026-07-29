"""Shared mapping from FOCUS-named columns (PascalCase) to a FocusRecord.

The row-based counterpart of :mod:`flashlight.focus.sql_mapping` (the same rules,
in DuckDB SQL) — used by connectors whose source isn't itself DuckDB-scannable, so
mapping has to happen in Python: ``databricks.py``'s vendored SQL query already
projects FOCUS-named columns, and each row goes through this. Connectors reading an
already-FOCUS-shaped file/blob directly (``aws_focus``, ``focus_file``) use the SQL
path instead — see ``ingest/base.py``'s ``Connector.fetch``/``ingest`` docstrings for
which one a new connector should implement. Tolerates the ``NULL`` / empty sentinels
real FOCUS exports use for missing values.
"""

from __future__ import annotations

import json
from typing import Any

from flashlight.focus.enums import ChargeClass
from flashlight.focus.model import FocusRecord
from flashlight.ingest.connectors._coerce import (
    to_charge_category,
    to_commitment_category,
    to_commitment_status,
    to_datetime,
    to_decimal,
    to_service_category,
)

_NULLISH = {"", "null", "none", "nan"}


def _s(value: Any) -> str | None:
    """Normalize a cell to a non-empty string or None (handles 'NULL')."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _NULLISH else text


def _f(value: Any) -> float | None:
    text = _s(value)
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _tags(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    # DuckDB surfaces a Parquet MAP column (how AWS FOCUS delivers Tags) as a list
    # of (key, value) pairs — coerce it back to a dict rather than dropping it.
    if isinstance(value, list):
        try:
            return {str(k): str(v) for k, v in value}
        except (TypeError, ValueError):
            return {}
    text = _s(value)
    if not text or text == "{}":
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def map_focus_row(row: dict[str, Any], source_connector: str) -> FocusRecord | None:
    """Map one FOCUS-columned row to a FocusRecord, or None if unusable."""
    charge_start = row.get("ChargePeriodStart")
    if _s(charge_start) is None:
        return None
    charge_end = row.get("ChargePeriodEnd") or charge_start
    charge_class = _s(row.get("ChargeClass"))

    return FocusRecord(
        provider_name=_s(row.get("ProviderName")) or "Unknown",
        billing_account_id=_s(row.get("BillingAccountId")) or "unknown",
        billing_account_name=_s(row.get("BillingAccountName")),
        sub_account_id=_s(row.get("SubAccountId")),
        sub_account_name=_s(row.get("SubAccountName")),
        billing_period_start=to_datetime(row.get("BillingPeriodStart") or charge_start).date(),
        billing_period_end=to_datetime(row.get("BillingPeriodEnd") or charge_end).date(),
        charge_period_start=to_datetime(charge_start),
        charge_period_end=to_datetime(charge_end),
        billing_currency=_s(row.get("BillingCurrency")) or "USD",
        billed_cost=to_decimal(row.get("BilledCost")),
        effective_cost=to_decimal(row.get("EffectiveCost")),
        list_cost=to_decimal(row.get("ListCost")),
        contracted_cost=to_decimal(row.get("ContractedCost")),
        charge_category=to_charge_category(_s(row.get("ChargeCategory"))),
        charge_class=ChargeClass.CORRECTION if charge_class == "Correction" else None,
        charge_description=_s(row.get("ChargeDescription")),
        service_category=to_service_category(_s(row.get("ServiceCategory"))),
        service_name=_s(row.get("ServiceName")) or "Unknown",
        sku_id=_s(row.get("SkuId")),
        region_id=_s(row.get("RegionId")),
        resource_id=_s(row.get("ResourceId")),
        resource_name=_s(row.get("ResourceName")),
        resource_type=_s(row.get("ResourceType")),
        consumed_quantity=_f(row.get("ConsumedQuantity")),
        consumed_unit=_s(row.get("ConsumedUnit")),
        tags=_tags(row.get("Tags")),
        commitment_discount_id=_s(row.get("CommitmentDiscountId")),
        commitment_discount_type=_s(row.get("CommitmentDiscountType")),
        commitment_discount_category=to_commitment_category(_s(row.get("CommitmentDiscountCategory"))),
        commitment_discount_name=_s(row.get("CommitmentDiscountName")),
        commitment_discount_status=to_commitment_status(_s(row.get("CommitmentDiscountStatus"))),
        commitment_discount_quantity=_f(row.get("CommitmentDiscountQuantity")),
        commitment_discount_unit=_s(row.get("CommitmentDiscountUnit")),
        invoice_id=_s(row.get("InvoiceId")),
        invoice_issuer_name=_s(row.get("InvoiceIssuerName")),
        x_source_connector=source_connector,
    )
