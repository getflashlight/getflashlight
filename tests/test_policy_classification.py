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
        # auto-stop within the 30-min default → compliant
        _rec("wh-autostop", EntityType.SQL_WAREHOUSE, cause_detail={"auto_stop_minutes": 10}),
        # auto-stop set but far over the policy → non_compliant (presence isn't enough)
        _rec("wh-slow-autostop", EntityType.SQL_WAREHOUSE,
             cause_detail={"auto_stop_minutes": 240}),
        # auto-stop telemetry unmeasured → not_applicable, never a false violation
        _rec("wh-unmeasured-autostop", EntityType.SQL_WAREHOUSE, cause_detail={}),
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

    assert idx[("wh-autostop", "warehouse_auto_stop")]["status"] == "compliant"
    assert idx[("wh-slow-autostop", "warehouse_auto_stop")]["status"] == "non_compliant"
    assert idx[("wh-unmeasured-autostop", "warehouse_auto_stop")]["status"] == "not_applicable"

    # jobs get no interactive/sql_warehouse-scoped rules at all
    assert not any(k[0] == "job-1" for k in idx)
    # sql_warehouse entities get no cluster-scoped rules
    assert not any(
        k[0] in ("wh-tagged", "wh-untagged")
        and k[1] in ("auto_terminate", "autoscaling", "cluster_policy_assigned")
        for k in idx
    )


def _write_policies(home, body: str) -> None:  # type: ignore[no-untyped-def]
    from flashlight.efficiency.policy_config import get_thresholds
    from flashlight.lake import paths

    paths.config_dir().mkdir(parents=True, exist_ok=True)
    paths.policies_path().write_text(body)
    get_thresholds.cache_clear()


def test_auto_terminate_honours_configured_ceiling(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A timeout that's set but longer than the org policy is a violation, not a pass."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    _write_policies(lake_home, "thresholds:\n  max_auto_termination_minutes: 45\n")
    metrics.write_efficiency(
        _WINDOW,
        [
            _rec("cl-brisk", EntityType.INTERACTIVE,
                 cause_detail={"auto_termination_minutes": 30}),
            _rec("cl-sluggish", EntityType.INTERACTIVE,
                 cause_detail={"auto_termination_minutes": 120}),
        ],
    )
    build_gold()
    idx = _by_entity_category(query_view("policy.policy_record"))

    assert idx[("cl-brisk", "auto_terminate")]["status"] == "compliant"
    assert idx[("cl-sluggish", "auto_terminate")]["status"] == "non_compliant"
    # The threshold the verdict was measured against is visible in the row itself.
    assert "45" in str(idx[("cl-sluggish", "auto_terminate")]["detail"])


def test_underutilized_threshold_is_configurable(lake_home) -> None:  # type: ignore[no-untyped-def]
    """The waste pool reads the same policies.yml — a stricter bar flags more waste."""
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    _write_policies(lake_home, "thresholds:\n  underutilized_pct: 50\n")
    metrics.write_efficiency(
        _WINDOW,
        [_rec("cl-meh", EntityType.INTERACTIVE, utilization_pct=40.0, cause_detail={})],
    )
    build_gold()
    categories = {
        str(r["waste_category"])
        for r in query_view("efficiency.waste_record")
        if r["entity_id"] == "cl-meh"
    }
    # 40% utilization passes the default 20% bar but fails the configured 50% one.
    assert "underutilized" in categories


def test_missing_policies_file_uses_efficient_defaults(lake_home) -> None:  # type: ignore[no-untyped-def]
    """policies.yml is an optional override — absent means defaults, not an error."""
    from flashlight.efficiency.policy_config import PolicyThresholds, get_thresholds
    from flashlight.lake import paths

    assert not paths.policies_path().exists()
    assert get_thresholds() == PolicyThresholds()


def test_malformed_policies_file_is_loud(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Silently defaulting would change every classification without telling anyone."""
    from flashlight.efficiency.policy_config import get_thresholds

    _write_policies(lake_home, "thresholds:\n  max_auto_termination_minutes: not-a-number\n")
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        get_thresholds()


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
