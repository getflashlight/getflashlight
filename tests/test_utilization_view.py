"""End-to-end: EfficiencyRecord → metrics Parquet → GOLD utilization view.

Covers 055_gold_utilization.sql, the "how well is my infra used?" surface. The riskiest
parts are the three-way coverage classification (measured / not_applicable / unmeasured)
and the primary-signal resolution, since both encode honesty rules that a plausible-looking
simplification would break — so every branch gets a fires case AND a does-not-fire case.

Read via ``query_view``, not raw DuckDB, so each test also validates the ViewSpec in
transform/catalog.py: a column missing from its dimensions/measures tuples is invisible to
MCP and to the assistant agent even when it is present in the Parquet.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from flashlight.core.settings import get_settings
from flashlight.efficiency.model import EfficiencyRecord, EntityType
from flashlight.ingest.base import IngestWindow

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
_MONTH = date(2026, 5, 1)


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(entity_id: str, entity_type: EntityType, cost: str, **kw: object) -> EfficiencyRecord:
    return EfficiencyRecord(
        provider_name="Databricks",
        charge_month=_MONTH,
        entity_type=entity_type,
        entity_id=entity_id,
        billed_cost=Decimal(cost),
        x_source_connector="databricks",
        **kw,
    )


def _build(records: list[EfficiencyRecord]) -> dict[str, dict[str, Any]]:
    """Write the records, build GOLD, return the utilization view keyed by entity_id."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    assert metrics.write_efficiency(_WINDOW, records) == len(records)
    build_gold()
    rows = query_view("efficiency.utilization_entity_month")
    return {str(r["entity_id"]): r for r in rows}


def test_measurement_status_separates_cannot_from_did_not(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The core honesty rule: 'no utilization obtainable' != 'telemetry did not arrive'."""
    idx = _build(
        [
            _rec("job-measured", EntityType.JOB, "100", utilization_pct=45.0, activity_count=12),
            # Serverless notebooks *could* carry CPU telemetry; this one didn't.
            _rec("nb-silent", EntityType.NOTEBOOK, "40"),
            # A SQL warehouse never can — it is shared compute (see EntityType docs).
            _rec("wh-shared", EntityType.SQL_WAREHOUSE, "80", cause_detail={"cache_hit_pct": 12.0}),
            _rec("qp-shape", EntityType.QUERY_PATTERN, "0",
                 cause_detail={"pct_runs_spilling": 30.0}),
            _rec("tbl-cold", EntityType.TABLE, "0", cause_detail={"days_since_last_access": 200}),
        ]
    )

    assert idx["job-measured"]["measurement_status"] == "measured"
    assert idx["nb-silent"]["measurement_status"] == "unmeasured"
    # These three must NOT read as "unmeasured" — that would imply a fixable data gap.
    assert idx["wh-shared"]["measurement_status"] == "not_applicable"
    assert idx["qp-shape"]["measurement_status"] == "not_applicable"
    assert idx["tbl-cold"]["measurement_status"] == "not_applicable"


def test_activity_status_distinguishes_zero_from_silence(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Zero activity is a measurement (and drives the idle rule); NULL is silence."""
    idx = _build(
        [
            _rec("job-zero", EntityType.JOB, "50", activity_count=0),
            _rec("job-quiet", EntityType.JOB, "50"),
        ]
    )

    assert idx["job-zero"]["activity_status"] == "measured"
    assert idx["job-zero"]["activity_count"] == 0
    assert idx["job-quiet"]["activity_status"] == "unmeasured"
    assert idx["job-quiet"]["activity_count"] is None


def test_saturated_readings_are_flagged_not_praised(lake_home) -> None:  # type: ignore[no-untyped-def]
    """~60% of real readings are exactly 100.0 — a ceiling artifact, not right-sizing."""
    idx = _build(
        [
            _rec("job-pegged", EntityType.JOB, "100", utilization_pct=100.0),
            _rec("job-near", EntityType.JOB, "100", utilization_pct=99.6),
            _rec("job-real", EntityType.JOB, "100", utilization_pct=45.0),
            _rec("nb-silent", EntityType.NOTEBOOK, "10"),
        ]
    )

    assert idx["job-pegged"]["is_saturated_reading"] is True
    assert idx["job-near"]["is_saturated_reading"] is True
    assert idx["job-real"]["is_saturated_reading"] is False
    # Unmeasured must be NULL, not False — False would assert "we looked, it wasn't pegged".
    assert idx["nb-silent"]["is_saturated_reading"] is None


def test_primary_signal_priority_and_fallback(lake_home) -> None:  # type: ignore[no-untyped-def]
    idx = _build(
        [
            # Both present → the priority pick wins, the loser lands in secondary_signals.
            _rec("tbl-both", EntityType.TABLE, "0",
                 cause_detail={"days_since_last_access": 200, "stats_off_pct": 90.0}),
            # Only the fallback present → it becomes primary.
            _rec("tbl-stats", EntityType.TABLE, "0", cause_detail={"stats_off_pct": 90.0}),
            _rec("job-cpu", EntityType.JOB, "10",
                 utilization_pct=40.0, cause_detail={"max_cpu_pct": 70.0, "max_mem_pct": 55.0}),
            _rec("job-mem", EntityType.JOB, "10",
                 utilization_pct=40.0, cause_detail={"max_mem_pct": 55.0}),
        ]
    )

    both = idx["tbl-both"]
    assert both["primary_signal_name"] == "days_since_last_access"
    assert both["primary_signal_value"] == pytest.approx(200.0)
    assert both["primary_signal_unit"] == "days"
    assert both["primary_signal_direction"] == "lower_is_better"
    assert "stats off" in str(both["secondary_signals"])

    assert idx["tbl-stats"]["primary_signal_name"] == "stats_off_pct"
    assert idx["tbl-stats"]["primary_signal_unit"] == "pct"

    assert idx["job-cpu"]["primary_signal_name"] == "max_cpu_pct"
    assert idx["job-cpu"]["primary_signal_value"] == pytest.approx(70.0)
    assert "mem 55" in str(idx["job-cpu"]["secondary_signals"])
    assert idx["job-mem"]["primary_signal_name"] == "max_mem_pct"


def test_signal_absent_reports_nothing_rather_than_a_default(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Guards the CASE-on-NULL trap: a notebook must not inherit a direction it never had."""
    idx = _build([_rec("nb-bare", EntityType.NOTEBOOK, "10", cause_detail={"photon": True})])

    row = idx["nb-bare"]
    assert row["primary_signal_name"] is None
    assert row["primary_signal_value"] is None
    assert row["primary_signal_unit"] is None
    assert row["primary_signal_direction"] is None


def test_duration_share_is_neutral_not_a_health_reading(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A user's share of warehouse time is attribution, not good or bad."""
    idx = _build(
        [
            _rec("wh-user", EntityType.SQL_WAREHOUSE_USER, "30",
                 cause_detail={"duration_share_pct": 40.0}),
            _rec("wh-cache", EntityType.SQL_WAREHOUSE, "30", cause_detail={"cache_hit_pct": 12.0}),
            _rec("qp-spill", EntityType.QUERY_PATTERN, "0",
                 cause_detail={"pct_runs_spilling": 30.0}),
        ]
    )

    assert idx["wh-user"]["primary_signal_direction"] == "neutral"
    assert idx["wh-cache"]["primary_signal_direction"] == "higher_is_better"
    assert idx["qp-spill"]["primary_signal_direction"] == "lower_is_better"


def test_cost_per_native_unit_only_for_a_positive_quantity(lake_home) -> None:  # type: ignore[no-untyped-def]
    idx = _build(
        [
            _rec("job-dbu", EntityType.JOB, "50", native_quantity=100.0, native_unit="DBU"),
            _rec("job-zeroq", EntityType.JOB, "50", native_quantity=0.0, native_unit="DBU"),
            # A corrective negative quantity must not yield a negative rate.
            _rec("job-negq", EntityType.JOB, "50", native_quantity=-5.0, native_unit="DBU"),
            _rec("job-noq", EntityType.JOB, "50"),
        ]
    )

    assert idx["job-dbu"]["cost_per_native_unit"] == pytest.approx(0.5)
    assert idx["job-dbu"]["native_unit"] == "dbu", "case is folded so DBU and dbu are one unit"
    assert idx["job-zeroq"]["cost_per_native_unit"] is None
    assert idx["job-negq"]["cost_per_native_unit"] is None
    assert idx["job-noq"]["cost_per_native_unit"] is None


def test_rule_flags_come_from_the_baked_pool_not_a_local_threshold(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The new fact this view makes expressible: measured, healthy, and no rule fired.

    The job rules partition utilization cleanly (see waste_rules.py): `underutilized`
    fires at <= 20, `job_low_utilization` in the 20-60 band, nothing at >= 60. So a job at
    75% is the case that is invisible in waste_record but present here — which is the
    entire reason this view exists.
    """
    idx = _build(
        [
            # 10% util consistently → `underutilized` fires, high confidence.
            _rec("job-under", EntityType.JOB, "100", utilization_pct=10.0, activity_count=5,
                 cause_detail={"pct_runs_underutilized": 0.9}),
            # 45% → flagged, but by the softer `job_low_utilization` band, NOT underutilized.
            _rec("job-band", EntityType.JOB, "100", utilization_pct=45.0, activity_count=5),
            # 75% → no rule fires anywhere. Only this view can see it.
            _rec("job-fine", EntityType.JOB, "100", utilization_pct=75.0, activity_count=5),
        ]
    )

    under = idx["job-under"]
    assert under["is_flagged_underutilized"] is True
    assert "underutilized" in str(under["waste_categories"])

    # is_flagged_underutilized is specifically about that one category, not "any finding".
    band = idx["job-band"]
    assert band["is_flagged_underutilized"] is False
    assert band["waste_categories"] == "job_low_utilization"

    fine = idx["job-fine"]
    assert fine["measurement_status"] == "measured"
    assert fine["is_flagged_underutilized"] is False
    assert fine["waste_categories"] is None


def test_view_is_empty_not_broken_without_any_efficiency_pull(lake_home) -> None:  # type: ignore[no-untyped-def]
    """GOLD must build on a lake with no efficiency data at all (typed-empty fallback)."""
    from datetime import UTC, datetime

    from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
    from flashlight.focus.model import FocusRecord
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    bronze.write_window(
        "t",
        _WINDOW,
        [
            FocusRecord(
                provider_name=ProviderName.AWS,
                billing_account_id="acct",
                billing_period_start=_MONTH,
                billing_period_end=date(2026, 6, 1),
                charge_period_start=datetime(2026, 5, 1, tzinfo=UTC),
                charge_period_end=datetime(2026, 5, 1, 1, tzinfo=UTC),
                billed_cost=Decimal("1"),
                effective_cost=Decimal("1"),
                list_cost=Decimal("1"),
                charge_category=ChargeCategory.USAGE,
                service_category=ServiceCategory.COMPUTE,
                service_name="AmazonEC2",
                x_source_connector="t",
            )
        ],
        ingest_run_id="r1",
    )
    build_gold()

    assert query_view("efficiency.utilization_entity_month") == []
