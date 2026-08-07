"""Helpers to coerce free-form provider values into FOCUS controlled vocab."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from flashlight.focus.enums import (
    ChargeCategory,
    CommitmentDiscountCategory,
    CommitmentDiscountStatus,
    PricingCategory,
    ServiceCategory,
)


def to_service_category(value: str | None) -> ServiceCategory:
    """Best-effort map a provider service-category string to the FOCUS enum."""
    if not value:
        return ServiceCategory.OTHER
    try:
        return ServiceCategory(value)
    except ValueError:
        return ServiceCategory.OTHER


def to_charge_category(value: str | None) -> ChargeCategory:
    """Map a provider charge-category string to the FOCUS enum (default Usage)."""
    if not value:
        return ChargeCategory.USAGE
    try:
        return ChargeCategory(value)
    except ValueError:
        return ChargeCategory.USAGE


def to_commitment_category(value: str | None) -> CommitmentDiscountCategory | None:
    """Map a commitment-discount-category string to the FOCUS enum, or None (Conditional field)."""
    if not value:
        return None
    try:
        return CommitmentDiscountCategory(value)
    except ValueError:
        return None


def to_commitment_status(value: str | None) -> CommitmentDiscountStatus | None:
    """Map a commitment-discount-status string to the FOCUS enum, or None (Conditional field)."""
    if not value:
        return None
    try:
        return CommitmentDiscountStatus(value)
    except ValueError:
        return None


def to_pricing_category(value: str | None) -> PricingCategory | None:
    """Map a pricing-category string to the FOCUS enum, or None (Conditional field).

    Unrecognized rather than absent still resolves to ``None``, not ``OTHER``: a
    provider value we can't parse is a mapping gap, not FOCUS's own "priced in a way
    not covered by the other categories" case — coercing it to ``OTHER`` would claim
    provider intent we don't actually have.
    """
    if not value:
        return None
    try:
        return PricingCategory(value)
    except ValueError:
        return None


def to_decimal(value: object) -> Decimal:
    """Parse a cost value to Decimal, defaulting to 0 on bad/empty input."""
    if value is None or value == "":
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def to_datetime(value: object) -> datetime:
    """Parse an ISO-ish timestamp/date into a datetime."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).replace("Z", "+00:00")
    return datetime.fromisoformat(text)
