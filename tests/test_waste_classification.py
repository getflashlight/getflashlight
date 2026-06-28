"""End-to-end: EfficiencyRecord → metrics Parquet → GOLD waste classification.

Exercises the riskiest part of the waste plane — the classification SQL
(050_gold_waste.sql) and the metrics register/COPY path — against a real in-memory
DuckDB over real Parquet. No warehouse needed: the connector's warehouse pull is the
only un-unit-testable piece; everything downstream of EfficiencyRecord is covered here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest

from auralake.core.settings import get_settings
from auralake.efficiency.model import EfficiencyRecord, EntityType
from auralake.ingest.base import IngestWindow

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))
_MONTH = date(2026, 5, 1)


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AURALAKE_HOME", str(tmp_path))
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


def _by_entity_category(rows: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, object]]:
    return {(str(r["entity_id"]), str(r["waste_category"])): r for r in rows}


def test_waste_classification(lake_home) -> None:  # type: ignore[no-untyped-def]
    from auralake.gold.reader import query_view
    from auralake.lake import metrics
    from auralake.transform.runner import build_gold

    records = [
        # underutilized job: 10% util, consistently → high confidence, recoverable 90% of cost
        _rec("job-under", EntityType.JOB, "100", utilization_pct=10.0, activity_count=5,
             cause_detail={"pct_runs_underutilized": 0.9}),
        # idle: billed but zero activity → full cost recoverable
        _rec("job-idle", EntityType.JOB, "50", activity_count=0),
        # placement: any all-purpose spend → OPPORTUNITY, 70% of cost (no activity gate)
        _rec("cl-interactive", EntityType.INTERACTIVE, "200", activity_count=10),
        # underutilized interactive CLUSTER: cluster-level util is honest at cluster grain
        _rec("cl-under", EntityType.INTERACTIVE, "120", utilization_pct=15.0),
        # SQL warehouse: shared, no per-entity util → no underutilized AND no placement
        _rec("wh-1", EntityType.SQL_WAREHOUSE, "300"),
        # failed runs: util healthy, but failed_cost present
        _rec("job-failed", EntityType.JOB, "80", utilization_pct=50.0, activity_count=3,
             cause_detail={"failed_cost": 20.0}),
        # photon-no-gain: photon on a low-util job → candidate, premium share
        _rec("job-photon", EntityType.JOB, "60", utilization_pct=10.0, activity_count=2,
             cause_detail={"photon": True, "pct_runs_underutilized": 0.5}),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("efficiency.waste_record")
    idx = _by_entity_category(rows)

    # underutilized: recoverable = cost × (1 − util) = 100 × 0.9, high confidence
    under = idx[("job-under", "underutilized")]
    assert under["recoverable_cost"] == pytest.approx(90.0)
    assert under["confidence"] == "high"
    assert under["lens"] == "WASTE"

    # idle: full billed cost
    assert idx[("job-idle", "idle")]["recoverable_cost"] == pytest.approx(50.0)

    # placement: 70% of cost, opportunity lens, candidate (all-purpose, no activity gate)
    placement = idx[("cl-interactive", "placement")]
    assert placement["recoverable_cost"] == pytest.approx(140.0)
    assert placement["lens"] == "OPPORTUNITY"
    assert placement["confidence"] == "candidate"

    # interactive CLUSTER with cluster-level util → underutilized is honest at cluster grain
    under_cl = idx[("cl-under", "underutilized")]
    assert under_cl["recoverable_cost"] == pytest.approx(120 * 0.85)
    # it is ALSO a placement candidate (different lens, different remedy)
    assert ("cl-under", "placement") in idx

    # failed: the failed_cost from cause_detail
    assert idx[("job-failed", "failed")]["recoverable_cost"] == pytest.approx(20.0)

    # photon-no-gain: premium share (1 − 1/2.9) of cost; candidate (pct < 0.8)
    photon = idx[("job-photon", "photon_no_gain")]
    assert photon["recoverable_cost"] == pytest.approx(60 * (1 - 1 / 2.9), rel=1e-3)
    assert photon["confidence"] == "candidate"
    # the same low-util photon job is ALSO underutilized (additive rows)
    assert ("job-photon", "underutilized") in idx

    # honesty: a healthy job emits no underutilized row
    assert ("job-failed", "underutilized") not in idx
    # no utilization data (NULL) → no underutilized row (cl-interactive has util=None)
    assert ("cl-interactive", "underutilized") not in idx
    # SQL warehouse: no per-entity util → never underutilized, and never a placement
    # candidate (you can't move a warehouse to jobs compute)
    assert not any(k[0] == "wh-1" for k in idx)


def test_summary_rolls_up(lake_home) -> None:  # type: ignore[no-untyped-def]
    from auralake.gold.reader import query_view
    from auralake.lake import metrics
    from auralake.transform.runner import build_gold

    metrics.write_efficiency(
        _WINDOW,
        [
            _rec("a", EntityType.JOB, "100", activity_count=0),  # idle 100
            _rec("b", EntityType.INTERACTIVE, "200", activity_count=5),  # placement 140
        ],
    )
    build_gold()
    summary = {str(r["lens"]): r for r in query_view("efficiency.waste_summary_month")}
    assert summary["WASTE"]["recoverable_cost"] == pytest.approx(100.0)
    assert summary["OPPORTUNITY"]["recoverable_cost"] == pytest.approx(140.0)
