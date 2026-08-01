"""End-to-end: EfficiencyRecord → metrics Parquet → GOLD policy-compliance classification.

Same shape as test_waste_classification.py, but for the policy-compliance plane
(070_gold_policy.sql / policy_rules.py) — every ACTIVE rule emits one row per
applicable entity every month (compliant/non_compliant/not_applicable), not just
violations.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

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


def _rec(entity_id: str, entity_type: EntityType, **kw: object) -> EfficiencyRecord:
    return EfficiencyRecord(
        provider_name="Databricks",
        charge_month=_MONTH,
        entity_type=entity_type,
        entity_id=entity_id,
        billed_cost=Decimal("100"),
        x_source_connector="databricks",
        **kw,
    )


def _by_entity_category(rows: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, object]]:
    return {(str(r["entity_id"]), str(r["policy_category"])): r for r in rows}


def test_policy_classification(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    records = [
        # no auto-termination → non_compliant
        _rec("cl-noautoterm", EntityType.INTERACTIVE, cause_detail={}),
        # has auto-termination → compliant
        _rec("cl-autoterm", EntityType.INTERACTIVE,
             cause_detail={"auto_termination_minutes": 30}),
        # real autoscale range → compliant
        _rec("cl-autoscale", EntityType.INTERACTIVE,
             cause_detail={"min_autoscale_workers": 2, "max_autoscale_workers": 8}),
        # fixed-size (no range) → non_compliant
        _rec("cl-fixed", EntityType.INTERACTIVE, cause_detail={}),
        # cluster policy assigned → compliant
        _rec("cl-policy", EntityType.INTERACTIVE, cause_detail={"policy_id": "pol-123"}),
        # no cluster policy → non_compliant
        _rec("cl-nopolicy", EntityType.INTERACTIVE, cause_detail={}),
        # tagged cluster → compliant
        _rec("cl-tagged", EntityType.INTERACTIVE, cause_detail={"tag_count": 3}),
        # confirmed zero tags → non_compliant
        _rec("cl-untagged", EntityType.INTERACTIVE, cause_detail={"tag_count": 0}),
        # tag telemetry unmeasured (key absent) → not_applicable
        _rec("cl-unmeasured-tags", EntityType.INTERACTIVE, cause_detail={}),
        # tagged warehouse → compliant
        _rec("wh-tagged", EntityType.SQL_WAREHOUSE, cause_detail={"tag_count": 1}),
        # untagged warehouse → non_compliant
        _rec("wh-untagged", EntityType.SQL_WAREHOUSE, cause_detail={"tag_count": 0}),
        # job entity — no policy rule applies to jobs (interactive/sql_warehouse only)
        _rec("job-1", EntityType.JOB, cause_detail={}),
    ]
    assert metrics.write_efficiency(_WINDOW, records) == len(records)

    build_gold()
    rows = query_view("policy.policy_record")
    idx = _by_entity_category(rows)

    assert idx[("cl-noautoterm", "auto_terminate")]["status"] == "non_compliant"
    assert idx[("cl-autoterm", "auto_terminate")]["status"] == "compliant"

    assert idx[("cl-autoscale", "autoscaling")]["status"] == "compliant"
    assert idx[("cl-fixed", "autoscaling")]["status"] == "non_compliant"

    assert idx[("cl-policy", "cluster_policy_assigned")]["status"] == "compliant"
    assert idx[("cl-nopolicy", "cluster_policy_assigned")]["status"] == "non_compliant"

    assert idx[("cl-tagged", "cluster_tagging")]["status"] == "compliant"
    assert idx[("cl-untagged", "cluster_tagging")]["status"] == "non_compliant"
    assert idx[("cl-unmeasured-tags", "cluster_tagging")]["status"] == "not_applicable"

    assert idx[("wh-tagged", "warehouse_tagging")]["status"] == "compliant"
    assert idx[("wh-untagged", "warehouse_tagging")]["status"] == "non_compliant"

    # jobs get no interactive/sql_warehouse-scoped rules at all
    assert not any(k[0] == "job-1" for k in idx)
    # sql_warehouse entities get no cluster-scoped rules
    assert not any(
        k[0] in ("wh-tagged", "wh-untagged")
        and k[1] in ("auto_terminate", "autoscaling", "cluster_policy_assigned")
        for k in idx
    )


def test_policy_summary_rolls_up(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    metrics.write_efficiency(
        _WINDOW,
        [
            _rec("cl-a", EntityType.INTERACTIVE,
                 cause_detail={"auto_termination_minutes": 30}),
            _rec("cl-b", EntityType.INTERACTIVE, cause_detail={}),
        ],
    )
    build_gold()
    rows = {
        (str(r["policy_category"]), str(r["status"])): r
        for r in query_view("policy.policy_summary_month")
    }
    assert rows[("auto_terminate", "compliant")]["entity_count"] == 1
    assert rows[("auto_terminate", "non_compliant")]["entity_count"] == 1
