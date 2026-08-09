"""Pure-function tests for the Efficiency & Waste tab's newer panels.

Exercises the Efficiency action-potential trend, resolution tracking, and the shared
conservative action roll-up used by both the homepage and action queue.
Same "test the pure computation, not the NiceGUI rendering" split as
``test_rule_coverage.py``'s ``rule_coverage_rows``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from flashlight.dashboard.summary import action_group_rows, entity_action_rows
from flashlight.dashboard.views.efficiency_waste import (
    _trend_by_month,
    completed_record_months,
    entity_finding_rows,
    mom_recoverable_delta,
)

# ── mom_recoverable_delta ────────────────────────────────────────────────────────────


def _waste_records(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "entity_id": f"fixture-{i}",
                "entity_type": "job",
                "confidence": "candidate",
                "billed_cost": row.get("recoverable_cost", 0.0),
                **row,
            }
            for i, row in enumerate(rows)
        ]
    )


def test_mom_delta_is_none_with_fewer_than_two_months() -> None:
    records = _waste_records(
        [{"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 100.0}]
    )
    assert mom_recoverable_delta(records, ["2026-06-01"], "WASTE") is None


def test_completed_record_months_excludes_the_current_partial_month() -> None:
    records = _waste_records(
        [
            {"charge_month": "2026-06-01", "lens": "WASTE", "recoverable_cost": 100.0},
            {"charge_month": "2026-07-01", "lens": "WASTE", "recoverable_cost": 200.0},
            {"charge_month": "2026-08-01", "lens": "WASTE", "recoverable_cost": 10.0},
        ]
    )

    assert completed_record_months(records, date(2026, 8, 1)) == ["2026-06-01", "2026-07-01"]


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


def test_trend_and_delta_use_the_same_best_action_rollup() -> None:
    records = _waste_records(
        [
            {
                "charge_month": "2026-05-01",
                "entity_id": "job-1",
                "lens": "WASTE",
                "recoverable_cost": 60.0,
            },
            {
                "charge_month": "2026-06-01",
                "entity_id": "job-1",
                "lens": "WASTE",
                "recoverable_cost": 100.0,
            },
            {
                "charge_month": "2026-06-01",
                "entity_id": "job-1",
                "lens": "WASTE",
                "recoverable_cost": 40.0,
            },
        ]
    )

    trend = _trend_by_month(records, date(2026, 5, 1), date(2026, 6, 30))
    june = trend.loc[trend["charge_month"].astype(str) == "2026-06-01", "recoverable_cost"]
    assert float(june.iloc[0]) == pytest.approx(100.0)
    assert mom_recoverable_delta(
        records, ["2026-05-01", "2026-06-01"], "WASTE"
    ) == pytest.approx(40.0)


def test_trend_by_month_empty_when_nothing_in_range() -> None:
    records = _waste_records(
        [{"charge_month": "2026-01-01", "lens": "WASTE", "recoverable_cost": 10.0}]
    )
    trend = _trend_by_month(records, date(2026, 5, 1), date(2026, 6, 30))
    assert trend.empty


def test_trend_by_month_empty_input_is_empty_not_broken() -> None:
    assert _trend_by_month(pd.DataFrame(), date(2026, 5, 1), date(2026, 6, 30)).empty


# ── action_group_rows ────────────────────────────────────────────────────────────────


def test_action_groups_use_one_best_recommendation_per_entity_and_lens() -> None:
    """Two overlapping rule findings must not turn into an inflated Jobs savings total."""
    rows = _month_rows(
        [
            {
                "entity_id": "job-1",
                "entity_type": "job",
                "lens": "WASTE",
                "recoverable_cost": 100.0,
                "billed_cost": 200.0,
                "confidence": "high",
            },
            {
                "entity_id": "job-1",
                "entity_type": "job",
                "lens": "WASTE",
                "recoverable_cost": 60.0,
                "billed_cost": 200.0,
                "confidence": "candidate",
            },
            {
                "entity_id": "job-2",
                "entity_type": "job",
                "lens": "WASTE",
                "recoverable_cost": 40.0,
                "billed_cost": 80.0,
                "confidence": "candidate",
            },
            {
                "entity_id": "job-1",
                "entity_type": "job",
                "lens": "OPPORTUNITY",
                "recoverable_cost": 25.0,
                "billed_cost": 200.0,
                "confidence": "candidate",
            },
        ]
    )

    groups = action_group_rows(rows).set_index(["entity_type", "lens"])
    waste = groups.loc[("job", "WASTE")]
    assert waste["potential_savings"] == pytest.approx(140.0)
    assert waste["entities"] == 2
    assert waste["high_confidence"] == pytest.approx(100.0)
    assert groups.loc[("job", "OPPORTUNITY"), "potential_savings"] == pytest.approx(25.0)


def test_evidence_rows_reconcile_to_their_entity_action_potential() -> None:
    rows = _month_rows(
        [
            {
                "entity_id": "job-1",
                "entity_type": "job",
                "lens": "WASTE",
                "waste_category": "job_low_utilization",
                "recoverable_cost": 100.0,
                "billed_cost": 200.0,
            },
            {
                "entity_id": "job-1",
                "entity_type": "job",
                "lens": "WASTE",
                "waste_category": "photon_no_gain",
                "recoverable_cost": 60.0,
                "billed_cost": 200.0,
            },
        ]
    )

    entities = entity_action_rows(rows, "job", "WASTE")
    evidence = entity_finding_rows(rows, "job", "WASTE", "job-1")
    assert evidence["potential_savings"].sum() == pytest.approx(
        entities["potential_savings"].sum()
    )
    assert evidence["rule_estimate"].sum() == pytest.approx(160.0)


def _month_rows(rows: list[dict[str, object]]) -> pd.DataFrame:
    return pd.DataFrame(rows)
