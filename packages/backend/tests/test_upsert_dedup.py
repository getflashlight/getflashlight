from datetime import date, datetime
from decimal import Decimal

from auralake.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from auralake.focus.model import FocusRecord
from auralake.store.upsert import collapse_duplicates


def _rec(resource_id: str, cost: str) -> FocusRecord:
    c = Decimal(cost)
    return FocusRecord(
        provider_name=ProviderName.AWS,
        billing_account_id="acct",
        billing_period_start=date(2026, 6, 1),
        billing_period_end=date(2026, 6, 30),
        charge_period_start=datetime(2026, 6, 15),
        charge_period_end=datetime(2026, 6, 16),
        billed_cost=c,
        effective_cost=c,
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name="AmazonEC2",
        resource_id=resource_id,
        x_source_connector="t",
    )


def test_collapses_duplicate_keys_last_wins() -> None:
    # Two records with identical dimensions → same dedupe_key → must collapse to one,
    # keeping the last (a restatement), so ON CONFLICT never hits the same row twice.
    out = collapse_duplicates([_rec("i-1", "10"), _rec("i-1", "99")])
    assert len(out) == 1
    assert out[0].effective_cost == Decimal("99")


def test_keeps_distinct_keys() -> None:
    out = collapse_duplicates([_rec("i-1", "10"), _rec("i-2", "20")])
    assert len(out) == 2
