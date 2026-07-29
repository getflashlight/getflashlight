from datetime import date, datetime
from decimal import Decimal

from flashlight.focus.enums import (
    ChargeCategory,
    CommitmentDiscountCategory,
    CommitmentDiscountStatus,
    ComputeClass,
    ProviderName,
    ServiceCategory,
)
from flashlight.focus.model import FocusRecord


def _record(**overrides: object) -> FocusRecord:
    base: dict[str, object] = dict(
        provider_name=ProviderName.DATABRICKS,
        billing_account_id="acct-1",
        billing_period_start=date(2026, 6, 1),
        billing_period_end=date(2026, 6, 30),
        charge_period_start=datetime(2026, 6, 15, 0, 0),
        charge_period_end=datetime(2026, 6, 16, 0, 0),
        billed_cost=Decimal("10"),
        effective_cost=Decimal("9"),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.ANALYTICS,
        service_name="Databricks JOBS",
        sku_id="STANDARD_JOBS_COMPUTE",
        resource_id="cluster-123",
    )
    base.update(overrides)
    return FocusRecord(**base)


def test_dedupe_key_is_stable() -> None:
    assert _record().dedupe_key() == _record().dedupe_key()


def test_dedupe_key_differs_on_dimension_change() -> None:
    assert _record().dedupe_key() != _record(resource_id="cluster-456").dedupe_key()


def test_dedupe_key_changes_on_cost_change() -> None:
    # Two rows sharing every other field but differing in cost are two distinct
    # charges (e.g. two Reserved Instance purchases of the same SKU in the same
    # period, both with no ResourceId), not one row delivered twice — confirmed
    # against a real AWS export where the old cost-blind key silently collapsed
    # exactly this case. Must NOT collide.
    assert _record().dedupe_key() != _record(effective_cost=Decimal("99")).dedupe_key()


def test_currency_uppercased() -> None:
    assert _record(billing_currency="usd").billing_currency == "USD"


def test_default_compute_class() -> None:
    assert _record().x_compute_class == ComputeClass.NOT_APPLICABLE


def test_commitment_and_invoice_fields_default_none() -> None:
    r = _record()
    assert r.commitment_discount_id is None
    assert r.commitment_discount_category is None
    assert r.commitment_discount_status is None
    assert r.invoice_id is None
    assert r.invoice_issuer_name is None


def test_commitment_and_invoice_fields_roundtrip() -> None:
    r = _record(
        commitment_discount_id="cud-1",
        commitment_discount_type="SavingsPlan",
        commitment_discount_category=CommitmentDiscountCategory.SPEND,
        commitment_discount_name="prod-savings-plan",
        commitment_discount_status=CommitmentDiscountStatus.USED,
        commitment_discount_quantity=1.5,
        commitment_discount_unit="Hrs",
        invoice_id="inv-2026-06",
        invoice_issuer_name="Amazon Web Services",
    )
    assert r.commitment_discount_status == CommitmentDiscountStatus.USED
    assert r.invoice_id == "inv-2026-06"


def test_dedupe_key_changes_on_commitment_status() -> None:
    a = _record(
        commitment_discount_id="cud-1", commitment_discount_status=CommitmentDiscountStatus.USED
    )
    b = _record(
        commitment_discount_id="cud-1", commitment_discount_status=CommitmentDiscountStatus.UNUSED
    )
    assert a.dedupe_key() != b.dedupe_key()
