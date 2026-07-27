from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal

from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.lake.bronze import dedupe


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


def test_collapses_identical_physical_row_first_wins() -> None:
    # Same physical source row appearing twice in a batch (e.g. overlapping re-ingest)
    # — identical in every field — → same dedupe_key → collapse to one (first wins —
    # dedupe() streams, so it can't look ahead), so ON CONFLICT never hits the same
    # row twice. This is idempotency de-dup, not correction-netting.
    out = list(dedupe([_rec("i-1", "10"), _rec("i-1", "10")]))
    assert len(out) == 1
    assert out[0].effective_cost == Decimal("10")


def test_keeps_distinct_keys() -> None:
    out = list(dedupe([_rec("i-1", "10"), _rec("i-2", "20")]))
    assert len(out) == 2


def test_same_resource_different_cost_is_not_a_duplicate() -> None:
    # Two rows sharing every "identifying" dimension but differing in cost are two
    # distinct charges, not one row delivered twice — e.g. an AWS account holding
    # multiple Reserved Instances of the same SKU in the same period, all with no
    # ResourceId (confirmed against a real export: the old curated-subset key
    # collapsed these into one, silently dropping the rest). Must NOT collapse.
    out = list(dedupe([_rec("i-1", "10"), _rec("i-1", "99")]))
    assert len(out) == 2
    assert {r.effective_cost for r in out} == {Decimal("10"), Decimal("99")}


def test_correction_records_are_not_collapsed() -> None:
    # ORIGINAL + RETRACTION share record_id but differ by record_type → distinct keys,
    # so both survive; their costs (retraction negative) net via SUM downstream.
    original = _rec("i-1", "10")
    original.x_record_id, original.x_record_type = "rec-1", "ORIGINAL"
    retraction = _rec("i-1", "-10")
    retraction.x_record_id, retraction.x_record_type = "rec-1", "RETRACTION"
    out = list(dedupe([original, retraction]))
    assert len(out) == 2
    assert sum(r.effective_cost for r in out) == Decimal("0")


def test_dedupe_streams_lazily() -> None:
    # dedupe() must be a generator, not a list-in-list-out helper — the whole point
    # is that write_window() can chunk its output without buffering the full pull.
    calls: list[str] = []

    def _records() -> Iterator[FocusRecord]:
        for resource_id in ("i-1", "i-2"):
            calls.append(resource_id)
            yield _rec(resource_id, "10")

    gen = dedupe(_records())
    assert calls == []  # nothing pulled yet
    next(gen)
    assert calls == ["i-1"]
