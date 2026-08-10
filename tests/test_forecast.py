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
    """Under 3 complete months the view reports no trend rather than a thin average."""
    days = [(date(2026, 5, 1) + timedelta(days=i), "10") for i in range(11)]
    trend = _by_kind(_build(days), "trend")

    assert len(trend) == 3  # next 3 months are present...
    assert all(r["forecast_cost"] is None for r in trend)  # ...but honestly empty
    # No complete month before May (the anchor), so the trailing window is empty.
    assert all(r["history_days"] == 0 for r in trend)


def test_trend_projects_a_flat_series_forward(lake_home) -> None:  # type: ignore[no-untyped-def]
    """With ≥3 complete months, a flat $10/day series holds that monthly mean forward."""
    days = [(date(2026, 1, 1) + timedelta(days=i), "10") for i in range(121)]
    rows = _build(days)
    trend = _by_kind(rows, "trend")

    assert len(trend) == 3
    # Last complete day is 2026-04-30 → anchor month April is excluded; trailing 3 are
    # Jan/Feb/Mar → (310 + 280 + 310) / 3 = 300. Projected months are May/Jun/Jul.
    assert all(r["history_days"] == 90 for r in trend)  # 31+28+31
    for row in trend:
        month = str(row["charge_month"])[:7]
        assert month in {"2026-05", "2026-06", "2026-07"}
        assert float(row["forecast_cost"]) == pytest.approx(300.0, rel=1e-6)


def test_trend_ignores_partial_current_month(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Tiny days in the still-accruing month must not pull the 3-month hold down."""
    days: list[tuple[date, str]] = []
    # Three complete months at $300/month ($10/day × 30/31/28).
    for month_start, n_days in (
        (date(2026, 1, 1), 31),
        (date(2026, 2, 1), 28),
        (date(2026, 3, 1), 31),
    ):
        days.extend((month_start + timedelta(days=i), "10") for i in range(n_days))
    # April is the anchor (last complete day Apr 10); early April days are $1 — a daily
    # OLS would collapse the projection; the monthly hold must stay at ~$300.
    days.extend((date(2026, 4, 1) + timedelta(days=i), "1") for i in range(11))
    trend = _by_kind(_build(days), "trend")

    assert len(trend) == 3
    assert all(r["forecast_cost"] is not None for r in trend)
    for row in trend:
        assert float(row["forecast_cost"]) == pytest.approx(300.0, rel=1e-6)


def test_forecast_never_projects_negative_spend(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Even a steeply declining history stays non-negative (clamp / positive trailing mean)."""
    days = [
        (date(2026, 1, 1) + timedelta(days=i), str(max(0, 200 - 2 * i))) for i in range(121)
    ]
    trend = _by_kind(_build(days), "trend")
    assert all(float(r["forecast_cost"]) >= 0 for r in trend)


# ── The dashboard's run-rate presentation gate ───────────────────────────────
# GOLD deliberately keeps a low-history run_rate figure (a 2-day mean is a valid mean),
# so the honesty gate lives in the KPI that renders it. These cover that gate directly —
def test_databricks_backing_costs_are_added_to_forecasts(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Storage and compute carry through to both forecast shapes, not just actual bars."""
    import pandas as pd

    from flashlight.dashboard.views import provider_focus

    backing = pd.DataFrame(
        {
            "charge_month": pd.to_datetime(
                ["2026-01-01", "2026-02-01", "2026-03-01", "2026-08-01"]
            ),
            "net_cost": [10.0, 20.0, 30.0, 100.0],
            "service_name": ["Databricks Storage"] * 4,
        }
    )
    monkeypatch.setattr(
        provider_focus,
        "_databricks_backing_monthly",
        lambda end, sm: backing[
            (backing["charge_month"] >= pd.Timestamp(sm))
            & (backing["charge_month"] <= pd.Timestamp(end))
        ],
    )

    run_rate, actual = provider_focus._databricks_backing_run_rate(date(2026, 8, 1), 5)
    assert actual == 100.0  # The current month's mapped AWS cost only.
    assert run_rate == 620.0  # $100 / 5 completed days × 31 days in August.

    trend = pd.DataFrame(
        {
            "charge_month": pd.to_datetime(["2026-05-01", "2026-06-01"]),
            "forecast_cost": [1.0, 1.0],
        }
    )
    # May forecasts from an April anchor, so the Jan–Mar backing mean is added.
    assert provider_focus._databricks_backing_trend_addition(trend) == 20.0
