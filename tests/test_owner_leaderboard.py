"""End-to-end: EfficiencyRecord → GOLD waste_by_owner_month (the leaderboard contract).

Covers 056_gold_owner_leaderboard.sql. The value of this view is entirely in its
normalization and its refusal to drop unowned spend, so that is what these tests pin:

* the three dirty-identity cases that would otherwise split one human across rows
  (casing, trailing whitespace from Redshift CHAR padding, bare service-principal UUIDs),
* the '(unattributed)' bucket surviving — on a real lake it is the single largest row,
  and a `WHERE owner IS NOT NULL` anywhere upstream would silently delete it,
* lens never collapsing, since WASTE and OPPORTUNITY are different remedies.

Read via ``query_view`` so the ViewSpec in transform/catalog.py is validated too.
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

# A bare 8-4-4-4-12 UUID, the shape Databricks service principals arrive as.
_SP_UUID = "8554dc05-1234-4abc-89ef-0123456789ab"


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


def _idle(entity_id: str, cost: str, **kw: object) -> EfficiencyRecord:
    """An idle job: fires exactly one rule (`idle`, WASTE, high confidence, full cost)."""
    return _rec(entity_id, EntityType.JOB, cost, activity_count=0, **kw)


def _build(
    records: list[EfficiencyRecord], *, dimension: str = "owner_user"
) -> list[dict[str, Any]]:
    from flashlight.gold.reader import query_view
    from flashlight.lake import metrics
    from flashlight.transform.runner import build_gold

    assert metrics.write_efficiency(_WINDOW, records) == len(records)
    build_gold()
    rows = query_view("efficiency.waste_by_owner_month")
    return [r for r in rows if r["owner_dimension"] == dimension]


def _by_key(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(r["owner_key"]): r for r in rows}


def test_one_human_is_one_row_despite_casing_and_padding(lake_home) -> None:  # type: ignore[no-untyped-def]
    """'Alice ' (CHAR-padded), 'alice' and 'ALICE' are the same person."""
    rows = _by_key(
        _build(
            [
                _idle("j1", "100", owner_user="Alice "),
                _idle("j2", "50", owner_user="alice"),
                _idle("j3", "10", owner_user="ALICE"),
            ]
        )
    )

    assert set(rows) == {"alice"}, "the three spellings must collapse to one key"
    alice = rows["alice"]
    assert alice["owner_kind"] == "user"
    assert alice["finding_count"] == 3
    assert alice["entity_count"] == 3
    assert float(alice["recoverable_cost"]) == pytest.approx(160.0)
    # Display uses the spelling on the costliest finding, so it stays recognizable.
    assert alice["owner_display"] == "Alice"


def test_service_principals_are_labelled_not_left_as_uuids(lake_home) -> None:  # type: ignore[no-untyped-def]
    rows = _by_key(
        _build([_idle("j1", "100", owner_user=_SP_UUID), _idle("j2", "20", owner_user="bob")])
    )

    sp = rows[_SP_UUID]
    assert sp["owner_kind"] == "service_principal"
    assert sp["owner_display"] == "Service principal 8554dc05"
    # The full UUID stays in owner_key so an agent can still filter on it exactly.
    assert sp["owner_key"] == _SP_UUID
    assert rows["bob"]["owner_kind"] == "user"


def test_the_unattributed_bucket_survives(lake_home) -> None:  # type: ignore[no-untyped-def]
    """Anti-regression for the largest row on a real lake (~$143k of shared compute)."""
    rows = _by_key(
        _build(
            [
                # Shared compute with no owner — absent by design, not missing.
                _rec("wh1", EntityType.SQL_WAREHOUSE, "500", activity_count=0),
                # An empty-string owner must land in the same bucket, not become an
                # owner whose name is blank.
                _idle("j1", "300", owner_user=""),
                _idle("j2", "20", owner_user="carol"),
            ]
        )
    )

    assert "(unattributed)" in rows, "unowned recoverable spend must never be dropped"
    unattributed = rows["(unattributed)"]
    assert unattributed["owner_kind"] == "unattributed_shared_compute"
    assert unattributed["owner_display"] == "Unattributed (shared compute)"
    assert float(unattributed["recoverable_cost"]) == pytest.approx(800.0)
    assert unattributed["finding_count"] == 2


def test_lens_is_never_collapsed(lake_home) -> None:  # type: ignore[no-untyped-def]
    """One owner with both a WASTE and an OPPORTUNITY finding gets two rows, not a sum."""
    rows = _build(
        [
            _idle("j1", "100", owner_user="dave"),
            # graviton_price_opportunity: an OPPORTUNITY-lens rule on an interactive cluster.
            _rec("c1", EntityType.INTERACTIVE, "200", owner_user="dave",
                 cause_detail={"worker_node_type": "m5.xlarge", "availability": "ON_DEMAND"}),
        ]
    )

    dave = [r for r in rows if r["owner_key"] == "dave"]
    lenses = {str(r["lens"]) for r in dave}
    assert lenses == {"WASTE", "OPPORTUNITY"}, f"expected both lenses as separate rows, got {dave}"


def test_high_confidence_is_a_subtotal_not_a_filter(lake_home) -> None:  # type: ignore[no-untyped-def]
    """A candidate finding still counts toward recoverable_cost, just not the high subtotal."""
    rows = _by_key(
        _build(
            [
                # idle → 'high' confidence, full cost recoverable.
                _idle("j-high", "100", owner_user="erin"),
                # job_low_utilization (20-60 band) → always 'candidate'.
                _rec("j-cand", EntityType.JOB, "100", owner_user="erin",
                     utilization_pct=40.0, activity_count=5),
            ]
        )
    )

    erin = rows["erin"]
    total = float(erin["recoverable_cost"])
    high = float(erin["recoverable_cost_high_confidence"])
    assert erin["finding_count"] == 2
    assert high == pytest.approx(100.0), "only the idle finding is high confidence"
    assert total > high, "the candidate finding must still be inside the total"


def test_the_project_dimension_is_present_with_its_own_unattributed_row(lake_home) -> None:  # type: ignore[no-untyped-def]
    """owner_project is ~1% populated on real data, so its Unattributed row dominates."""
    rows = _by_key(
        _build(
            [
                _idle("j1", "100", owner_user="frank", owner_project="marketing_spend"),
                _idle("j2", "900", owner_user="grace"),  # no project tag
            ],
            dimension="owner_project",
        )
    )

    assert set(rows) == {"marketing_spend", "(unattributed)"}
    assert rows["marketing_spend"]["owner_kind"] == "project"
    # A project tag is free text, never an identity — no service-principal kind here.
    assert rows["(unattributed)"]["owner_kind"] == "unattributed"
    assert rows["(unattributed)"]["owner_display"] == "Unattributed"
    assert float(rows["(unattributed)"]["recoverable_cost"]) == pytest.approx(900.0)


def test_view_is_empty_not_broken_without_any_efficiency_pull(lake_home) -> None:  # type: ignore[no-untyped-def]
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

    assert query_view("efficiency.waste_by_owner_month") == []
