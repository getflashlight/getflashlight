from datetime import date, datetime
from decimal import Decimal

from auralake.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from auralake.focus.model import FocusRecord
from auralake.lake.bronze import collapse_duplicates


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


def test_collapses_identical_physical_row_last_wins() -> None:
    # Same physical source row appearing twice in a batch (e.g. overlapping re-ingest)
    # → same dedupe_key → collapse to one (last wins), so ON CONFLICT never hits the
    # same row twice. This is idempotency de-dup, not correction-netting.
    out = collapse_duplicates([_rec("i-1", "10"), _rec("i-1", "99")])
    assert len(out) == 1
    assert out[0].effective_cost == Decimal("99")


def test_keeps_distinct_keys() -> None:
    out = collapse_duplicates([_rec("i-1", "10"), _rec("i-2", "20")])
    assert len(out) == 2


def test_correction_records_are_not_collapsed() -> None:
    # ORIGINAL + RETRACTION share record_id but differ by record_type → distinct keys,
    # so both survive; their costs (retraction negative) net via SUM downstream.
    original = _rec("i-1", "10")
    original.x_record_id, original.x_record_type = "rec-1", "ORIGINAL"
    retraction = _rec("i-1", "-10")
    retraction.x_record_id, retraction.x_record_type = "rec-1", "RETRACTION"
    out = collapse_duplicates([original, retraction])
    assert len(out) == 2
    assert sum(r.effective_cost for r in out) == Decimal("0")
