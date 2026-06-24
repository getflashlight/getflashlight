from datetime import date, datetime
from decimal import Decimal

from auralake.focus.enums import ChargeCategory, ComputeClass, ProviderName, ServiceCategory
from auralake.focus.model import FocusRecord


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


def test_dedupe_key_ignores_cost_changes() -> None:
    # A restatement (same dimensions, new cost) must collide so upsert corrects it.
    assert _record().dedupe_key() == _record(effective_cost=Decimal("99")).dedupe_key()


def test_currency_uppercased() -> None:
    assert _record(billing_currency="usd").billing_currency == "USD"


def test_default_compute_class() -> None:
    assert _record().x_compute_class == ComputeClass.NOT_APPLICABLE
