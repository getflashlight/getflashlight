"""End-to-end: BRONZE tags → GOLD spend_by_tag_key_month (the folded tag-key ranking).

Covers 036_gold_tag_keys.sql. Two things are worth pinning here, and the second is the
unusual one:

* the fold itself — `Epic`/`epic` and `app-long`/`app_long` must rank as one row, with the
  collision still visible via tag_key_variants/variant_count;
* that per-key net_cost **deliberately over-counts** relative to real tagged spend, because
  a resource with two tags belongs to both keys. There is a positive assertion for that
  below so nobody later "fixes" it into a percentage — the honest denominator lives in
  spend_tag_coverage_month, which counts each resource once.

Plus a no-regression guard that spend_by_tag_month still exposes the RAW spellings: that
difference is a tagging-consistency finding for agents, not noise to be tidied away.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from flashlight.core.settings import get_settings
from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow

_DAY = date(2026, 5, 4)
_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(cost: str, tags: dict[str, str], *, credit: bool = False) -> FocusRecord:
    amount = Decimal(cost)
    return FocusRecord(
        provider_name=ProviderName.AWS,
        billing_account_id="acct",
        billing_period_start=_DAY.replace(day=1),
        billing_period_end=_DAY.replace(day=1) + timedelta(days=27),
        charge_period_start=datetime(_DAY.year, _DAY.month, _DAY.day, tzinfo=UTC),
        charge_period_end=datetime(_DAY.year, _DAY.month, _DAY.day, 1, tzinfo=UTC),
        billed_cost=amount,
        effective_cost=amount,
        list_cost=amount,
        charge_category=ChargeCategory.CREDIT if credit else ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name="AmazonEC2",
        tags=tags,
        x_source_connector="t",
    )


def _build(records: list[FocusRecord]) -> dict[str, dict[str, Any]]:
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window("t", _WINDOW, records, ingest_run_id="r1")
    build_gold()
    return {
        str(r["tag_key_normalized"]): r for r in query_view("aws.spend_by_tag_key_month")
    }


def test_case_and_separator_variants_fold_into_one_row(lake_home) -> None:  # type: ignore[no-untyped-def]
    rows = _build(
        [
            _rec("100", {"Epic": "a"}),
            _rec("50", {"epic": "b"}),
            _rec("40", {"app-long": "x"}),
            _rec("10", {"app_long": "y"}),
            _rec("25", {"env": "prod"}),
        ]
    )

    epic = rows["epic"]
    assert epic["variant_count"] == 2
    assert epic["tag_key_variants"] == "Epic · epic"
    assert float(epic["net_cost"]) == pytest.approx(150.0), "both spellings' spend is combined"
    assert epic["tag_value_count"] == 2

    app = rows["app_long"]
    assert app["variant_count"] == 2
    assert app["tag_key_variants"] == "app-long · app_long"
    assert float(app["net_cost"]) == pytest.approx(50.0)

    # A consistently-spelled key is not reported as a collision.
    assert rows["env"]["variant_count"] == 1
    assert rows["env"]["tag_key_variants"] == "env"


def test_the_raw_keys_are_still_exposed_separately(lake_home) -> None:  # type: ignore[no-untyped-def]
    """No-regression: folding must not reach back into spend_by_tag_month."""
    from flashlight.gold.reader import query_view

    _build([_rec("100", {"Epic": "a"}), _rec("50", {"epic": "b"})])

    raw_keys = {str(r["tag_key"]) for r in query_view("aws.spend_by_tag_month")}
    assert {"Epic", "epic"} <= raw_keys, (
        "spend_by_tag_month must keep both spellings — the inconsistency is itself a finding"
    )


def test_multi_tagged_spend_is_counted_under_every_key_on_purpose(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The over-count is inherent to a per-key breakdown, so it is asserted, not hidden.

    If someone later divides net_cost by a locally-computed total to make a percentage, this
    test is what tells them the denominator is fake.
    """
    from flashlight.gold.reader import query_view

    rows = _build([_rec("100", {"team": "data", "env": "prod"})])

    assert float(rows["team"]["net_cost"]) == pytest.approx(100.0)
    assert float(rows["env"]["net_cost"]) == pytest.approx(100.0)
    per_key_total = sum(float(r["net_cost"]) for r in rows.values())

    coverage = query_view("aws.spend_tag_coverage_month")
    tagged_cost = float(coverage[0]["tagged_cost"])
    assert tagged_cost == pytest.approx(100.0), "coverage counts the resource once"
    assert per_key_total > tagged_cost, (
        "per-key net_cost is expected to exceed real tagged spend; use "
        "spend_tag_coverage_month.tagged_cost as the denominator instead"
    )


def test_credits_are_excluded_so_it_reconciles_with_coverage(lake_home) -> None:  # type: ignore[no-untyped-def]
    rows = _build(
        [
            _rec("100", {"team": "data"}),
            # A tagged credit: excluded here, matching spend_tag_coverage_month's denominator.
            _rec("-30", {"team": "data"}, credit=True),
        ]
    )

    assert float(rows["team"]["net_cost"]) == pytest.approx(100.0)
