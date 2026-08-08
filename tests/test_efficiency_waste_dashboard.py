"""Pure-function tests for the Efficiency & Waste tab's newer panels.

Covers the computation split out from :mod:`flashlight.dashboard.views.efficiency_waste`
for the three panels added to render GOLD views that were previously published but never
consumed by the dashboard: :func:`mom_recoverable_delta`/:func:`_trend_by_month` (the KPI
delta and trend chart over ``waste_record``), :func:`entities_for_owner` (the owner drill,
``waste_by_owner_month``), and :func:`resolution_summary` (``waste_resolution_month``).
Same "test the pure computation, not the NiceGUI rendering" split as
``test_rule_coverage.py``'s ``rule_coverage_rows``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from flashlight.dashboard.views.efficiency_waste import (
    _realized_savings_variant,
    _trend_by_month,
    entities_for_owner,
    mom_recoverable_delta,
    resolution_summary,
)

# ── mom_recoverable_delta ────────────────────────────────────────────────────────────


def _waste_records(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_mom_delta_is_none_with_fewer_than_two_months() -> None:
    records = _waste_records(
        [{"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 100.0}]
    )
    assert mom_recoverable_delta(records, ["2026-06-01"], "WASTE") is None


def test_mom_delta_compares_the_last_two_months_only() -> None:
    """A third, older month must not leak into the comparison."""
    records = _waste_records(
        [
            {"charge_month": "2026-04-01", "lens": "WASTE", "recoverable_cost": 9999.0},
            {"charge_month": "2026-05-01", "lens": "WASTE", "recoverable_cost": 100.0},
            {"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 150.0},
        ]
    )
    months = ["2026-04-01", "2026-05-01", "2026-06-01"]
    assert mom_recoverable_delta(records, months, "WASTE") == pytest.approx(50.0)


def test_mom_delta_never_mixes_lenses() -> None:
    """WASTE growing while OPPORTUNITY shrinks must not net out to a false "flat"."""
    records = _waste_records(
        [
            {"charge_month": "2026-05-01", "lens": "WASTE", "recoverable_cost": 100.0},
            {"charge_month": "2026-05-01", "lens": "OPPORTUNITY", "recoverable_cost": 500.0},
            {"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 300.0},
            {"charge_month": "2026-06-01", "lens": "OPPORTUNITY", "recoverable_cost": 500.0},
        ]
    )
    months = ["2026-05-01", "2026-06-01"]
    assert mom_recoverable_delta(records, months, "WASTE") == pytest.approx(200.0)
    assert mom_recoverable_delta(records, months, "OPPORTUNITY") == pytest.approx(0.0)


# ── _trend_by_month ──────────────────────────────────────────────────────────────────


def test_trend_by_month_drops_rows_outside_the_selected_range() -> None:
    records = _waste_records(
        [
            {"charge_month": "2026-01-01", "lens": "WASTE", "recoverable_cost": 10.0},
            {"charge_month": "2026-05-01", "lens": "WASTE", "recoverable_cost": 20.0},
            {"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 30.0},
        ]
    )
    trend = _trend_by_month(records, date(2026, 5, 1), date(2026, 6, 30))
    assert sorted(trend["charge_month"].astype(str).unique()) == ["2026-05-01", "2026-06-01"]
    assert set(trend["recoverable_cost"]) == {20.0, 30.0}


def test_trend_by_month_sums_within_a_month_and_lens() -> None:
    """Two entities in the same month/lens must roll into one point, not two."""
    records = _waste_records(
        [
            {"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 20.0},
            {"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 30.0},
            {"charge_month": "2026-06-01", "lens": "OPPORTUNITY", "recoverable_cost": 5.0},
        ]
    )
    trend = _trend_by_month(records, date(2026, 6, 1), date(2026, 6, 30))
    by_lens = trend.set_index("lens")["recoverable_cost"]
    assert by_lens["WASTE"] == pytest.approx(50.0)
    assert by_lens["OPPORTUNITY"] == pytest.approx(5.0)


def test_trend_by_month_empty_when_nothing_in_range() -> None:
    records = _waste_records(
        [{"charge_month": "2026-01-01", "lens": "WASTE", "recoverable_cost": 10.0}]
    )
    trend = _trend_by_month(records, date(2026, 5, 1), date(2026, 6, 30))
    assert trend.empty


def test_trend_by_month_empty_input_is_empty_not_broken() -> None:
    assert _trend_by_month(pd.DataFrame(), date(2026, 5, 1), date(2026, 6, 30)).empty


# ── entities_for_owner ───────────────────────────────────────────────────────────────


def _month_rows(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_entities_for_owner_folds_case_and_whitespace() -> None:
    """The click target is the *folded* key ("alice") — must match every raw spelling
    056_gold_owner_leaderboard.sql folds into it."""
    rows = _month_rows(
        [
            {"entity_id": "j1", "owner_user": "Alice ", "lens": "WASTE"},
            {"entity_id": "j2", "owner_user": "ALICE", "lens": "WASTE"},
            {"entity_id": "j3", "owner_user": "bob", "lens": "WASTE"},
        ]
    )
    matched = entities_for_owner(rows, "alice", "WASTE")
    assert set(matched["entity_id"]) == {"j1", "j2"}


def test_entities_for_owner_never_leaks_across_lens() -> None:
    rows = _month_rows(
        [
            {"entity_id": "j1", "owner_user": "carol", "lens": "WASTE"},
            {"entity_id": "j2", "owner_user": "carol", "lens": "OPPORTUNITY"},
        ]
    )
    assert set(entities_for_owner(rows, "carol", "WASTE")["entity_id"]) == {"j1"}
    assert set(entities_for_owner(rows, "carol", "OPPORTUNITY")["entity_id"]) == {"j2"}


def test_entities_for_owner_unattributed_matches_blank_and_null() -> None:
    """'(unattributed)' is the view's literal for shared compute with no owner — must
    match both a NULL owner_user and an empty-string one, never a real name."""
    rows = _month_rows(
        [
            {"entity_id": "wh1", "owner_user": None, "lens": "WASTE"},
            {"entity_id": "wh2", "owner_user": "", "lens": "WASTE"},
            {"entity_id": "j1", "owner_user": "dave", "lens": "WASTE"},
        ]
    )
    matched = entities_for_owner(rows, "(unattributed)", "WASTE")
    assert set(matched["entity_id"]) == {"wh1", "wh2"}


# ── resolution_summary ───────────────────────────────────────────────────────────────


def _resolution_rows(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_resolution_summary_of_empty_input_is_empty_not_broken() -> None:
    summary = resolution_summary(pd.DataFrame())
    assert summary["resolved_count"] == 0
    assert summary["open_count"] == 0
    assert summary["oldest_open_months"] is None


def test_resolution_summary_counts_resolved_and_open_separately() -> None:
    rows = _resolution_rows(
        [
            {
                "is_resolved": True,
                "realized_savings": 100.0,
                "first_seen_month": "2026-01-01",
                "last_seen_month": "2026-02-01",
                "recoverable_cost_at_last_seen": 50.0,
            },
            {
                "is_resolved": False,
                "realized_savings": None,
                "first_seen_month": "2026-03-01",
                "last_seen_month": "2026-06-01",
                "recoverable_cost_at_last_seen": 75.0,
            },
        ]
    )
    summary = resolution_summary(rows)
    assert summary["resolved_count"] == 1
    assert summary["open_count"] == 1
    assert summary["realized_savings_total"] == pytest.approx(100.0)
    assert summary["open_recoverable_total"] == pytest.approx(75.0)


def test_resolution_summary_realized_savings_can_be_negative_not_clamped() -> None:
    """A resolved finding whose entity's total billed cost rose for unrelated reasons
    must show that honestly, not clamp to zero and imply a saving that didn't happen."""
    rows = _resolution_rows(
        [
            {
                "is_resolved": True,
                "realized_savings": -40.0,
                "first_seen_month": "2026-01-01",
                "last_seen_month": "2026-01-01",
                "recoverable_cost_at_last_seen": 10.0,
            }
        ]
    )
    summary = resolution_summary(rows)
    assert summary["realized_savings_total"] == pytest.approx(-40.0)
    realized = float(summary["realized_savings_total"])  # type: ignore[arg-type]
    assert _realized_savings_variant(realized) == "increase"


def test_resolution_summary_oldest_open_counts_the_flagged_month_itself() -> None:
    """A finding first (and only) seen in the current month has been open 1 month, not 0
    — the +1 in resolution_summary's month-count exists for exactly this edge."""
    rows = _resolution_rows(
        [
            {
                "is_resolved": False,
                "realized_savings": None,
                "first_seen_month": "2026-06-01",
                "last_seen_month": "2026-06-01",
                "recoverable_cost_at_last_seen": 10.0,
            }
        ]
    )
    summary = resolution_summary(rows)
    assert summary["oldest_open_months"] == 1


def test_resolution_summary_oldest_open_spans_multiple_months() -> None:
    rows = _resolution_rows(
        [
            {
                "is_resolved": False,
                "realized_savings": None,
                "first_seen_month": "2026-01-01",
                "last_seen_month": "2026-06-01",
                "recoverable_cost_at_last_seen": 10.0,
            },
            {
                "is_resolved": False,
                "realized_savings": None,
                "first_seen_month": "2026-05-01",
                "last_seen_month": "2026-06-01",
                "recoverable_cost_at_last_seen": 20.0,
            },
        ]
    )
    summary = resolution_summary(rows)
    # Jan -> Jun inclusive is 6 months open, driven by the older of the two open rows.
    assert summary["oldest_open_months"] == 6


def test_resolution_summary_current_month_is_read_from_data_not_the_clock() -> None:
    """Anti-regression: must never fall back to date.today() — a lake ingested weeks ago
    would otherwise overstate every open finding's age."""
    rows = _resolution_rows(
        [
            {
                "is_resolved": False,
                "realized_savings": None,
                "first_seen_month": "2020-01-01",
                "last_seen_month": "2020-01-01",
                "recoverable_cost_at_last_seen": 10.0,
            }
        ]
    )
    summary = resolution_summary(rows)
    assert summary["oldest_open_months"] == 1
    assert pd.Timestamp(summary["current_month"]) == pd.Timestamp("2020-01-01")


# ── _realized_savings_variant ────────────────────────────────────────────────────────


def test_realized_savings_variant_is_the_opposite_sense_of_delta_variant() -> None:
    """Positive realized savings is good news (green), unlike a rising cost delta
    elsewhere on the tab, which is bad news (red) — see the function's own docstring."""
    assert _realized_savings_variant(100.0) == "savings"
    assert _realized_savings_variant(-40.0) == "increase"
    assert _realized_savings_variant(0.0) == "neutral"
