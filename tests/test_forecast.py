"""End-to-end: BRONZE daily spend → gold.spend_forecast_month.

The arithmetic is deliberately checkable by hand — a flat $10/day month must project
to exactly days_in_month × $10 — so a regression in the SQL shows up as a wrong number,
not just a missing view.
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


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(day: date, cost: str) -> FocusRecord:
    amount = Decimal(cost)
    return FocusRecord(
        provider_name=ProviderName.AWS,
        billing_account_id="acct",
        billing_period_start=day.replace(day=1),
        billing_period_end=day.replace(day=1) + timedelta(days=27),
        charge_period_start=datetime(day.year, day.month, day.day, tzinfo=UTC),
        charge_period_end=datetime(day.year, day.month, day.day, 1, tzinfo=UTC),
        billed_cost=amount,
        effective_cost=amount,
        list_cost=amount,
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name="AmazonEC2",
        x_source_connector="t",
    )


def _build(days: list[tuple[date, str]]) -> list[dict[str, Any]]:
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    first, last = days[0][0], days[-1][0]
    bronze.write_window(
        "t",
        IngestWindow(first, last),
        [_rec(day, cost) for day, cost in days],
        ingest_run_id="r1",
    )
    build_gold()
    return query_view("aws.spend_forecast_month")


def _by_kind(rows: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return sorted(
        (r for r in rows if r["forecast_kind"] == kind),
        key=lambda r: str(r["charge_month"]),
    )


def test_run_rate_projects_the_month_from_complete_days(lake_home) -> None:  # type: ignore[no-untyped-def]
    """10 delivered days at $10 → $10/day → a 31-day month lands at $310."""
    # 11 days written; the newest is dropped as partially-delivered, leaving 10.
    days = [(date(2026, 5, 1) + timedelta(days=i), "10") for i in range(11)]
    rows = _build(days)

    run_rate = _by_kind(rows, "run_rate")
    assert len(run_rate) == 1
    row = run_rate[0]
    assert str(row["charge_month"]).startswith("2026-05")
    assert row["history_days"] == 10
    assert float(row["forecast_cost"]) == pytest.approx(310.0)  # 31 days in May
    # Actuals keep the partial newest day — it is real billed money.
    assert float(row["actual_to_date"]) == pytest.approx(110.0)


def test_run_rate_ignores_the_partial_newest_day(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A half-delivered final day must not drag the projection down."""
    days = [(date(2026, 5, 1) + timedelta(days=i), "10") for i in range(10)]
    days.append((date(2026, 5, 11), "1"))  # partially delivered
    row = _by_kind(_build(days), "run_rate")[0]

    # Still $10/day: the $1 day is excluded from the rate, included in actuals.
    assert float(row["forecast_cost"]) == pytest.approx(310.0)
    assert float(row["actual_to_date"]) == pytest.approx(101.0)


def test_trend_is_null_without_enough_history(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Under 60 complete days the view reports no trend rather than fitted noise."""
    days = [(date(2026, 5, 1) + timedelta(days=i), "10") for i in range(11)]
    trend = _by_kind(_build(days), "trend")

    assert len(trend) == 3  # next 3 months are present...
    assert all(r["forecast_cost"] is None for r in trend)  # ...but honestly empty
    assert all(r["history_days"] == 10 for r in trend)


def test_trend_projects_a_flat_series_forward(lake_home) -> None:  # type: ignore[no-untyped-def]
    """With enough history, a flat $10/day series extrapolates to $10/day."""
    days = [(date(2026, 1, 1) + timedelta(days=i), "10") for i in range(121)]
    rows = _build(days)
    trend = _by_kind(rows, "trend")

    assert len(trend) == 3
    assert all(r["history_days"] == 120 for r in trend)
    # Last complete day is 2026-04-30, so the trend months are May/Jun/Jul 2026.
    expected = {"2026-05": 31 * 10.0, "2026-06": 30 * 10.0, "2026-07": 31 * 10.0}
    for row in trend:
        month = str(row["charge_month"])[:7]
        assert float(row["forecast_cost"]) == pytest.approx(expected[month], rel=1e-6)


def test_forecast_never_projects_negative_spend(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A steep downward slope extrapolates below zero long before that means anything."""
    days = [
        (date(2026, 1, 1) + timedelta(days=i), str(max(0, 200 - 2 * i))) for i in range(121)
    ]
    trend = _by_kind(_build(days), "trend")
    assert all(float(r["forecast_cost"]) >= 0 for r in trend)
