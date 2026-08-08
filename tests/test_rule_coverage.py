"""Rule-coverage scoping — which waste rules a provider can even be judged against.

The dangerous direction is over-inclusion: a rule listed for a provider that never
measured its signal renders as **"clean"**, i.e. "we checked and found nothing". That is a
worse lie than omitting the rule, and it's the failure the old hand-maintained
``redshift_focus._RULE_GROUPS`` map invited — it had no link back to the rule definitions
and nothing keeping the two in sync.
"""

from __future__ import annotations

import pandas as pd
import pytest

from flashlight.dashboard.views.efficiency_waste import (
    _split_dry_groups,
    rule_coverage_rows,
    status_dot_style,
)
from flashlight.efficiency.waste_rules import (
    WASTE_RULES,
    blocked_rules,
    coverage_groups,
    is_blocked,
)

# Rules keying on fields only Databricks' connector populates, on an entity_type the
# Redshift connector *does* emit. Scoping by entity_type alone would list every one of
# these on a Redshift cluster's coverage table as "clean" — claiming we checked cache
# reuse, disk spill and serverless pricing that were never measured.
_DATABRICKS_ONLY_ON_SHARED_ENTITY_TYPES = (
    "sql_warehouse_low_cache_reuse",  # cache_hit_pct
    "sql_warehouse_disk_spill",  # spill_query_count
    "sql_warehouse_serverless_pricing_gap",  # warehouse_type = 'SERVERLESS'
    "sql_warehouse_high_frequency_workload",  # warehouse_type = 'SERVERLESS'
)


def _categories(provider: str) -> set[str]:
    return {r.category for _, rules in coverage_groups(provider) for r in rules}


def test_every_rule_declares_its_evaluability_scope() -> None:
    """Structural: ``_COVERAGE`` is applied at import, so a WasteRule added without an
    entry raises rather than silently vanishing from every coverage table. Importing the
    module at all proves it; this pins the count so the guard can't be quietly removed."""
    assert len(WASTE_RULES) == 37
    assert sum(1 for r in WASTE_RULES if is_blocked(r)) == 2


@pytest.mark.parametrize("category", _DATABRICKS_ONLY_ON_SHARED_ENTITY_TYPES)
def test_databricks_only_rules_are_absent_from_aws_coverage(category: str) -> None:
    """The load-bearing honesty guard — see the module docstring."""
    assert category in _categories("Databricks"), "should be evaluable where it IS measured"
    assert category not in _categories("AWS"), (
        f"{category} reads a Databricks-only field; listing it for AWS renders 'clean' "
        "for a check that never ran"
    )


def test_redshift_rules_are_absent_from_databricks_coverage() -> None:
    """The mirror of the above: Databricks' table has no `redshift_*` or S3 rules."""
    databricks = _categories("Databricks")
    assert not [c for c in databricks if c.startswith("redshift_")]
    assert "s3_intelligent_tiering" not in databricks


def test_every_active_rule_is_evaluable_somewhere() -> None:
    """No active rule may be unreachable from every provider — that would be a rule the
    app runs but never reports coverage for."""
    reachable = _categories("Databricks") | _categories("AWS")
    active = {r.category for r in WASTE_RULES if not is_blocked(r)}
    assert active - reachable == set()


def test_blocked_rules_are_listed_apart_from_evaluated_ones() -> None:
    """A blocked rule is neither 'clean' nor 'no data' — it isn't evaluated at all, so it
    must never occupy a coverage row that implies a verdict."""
    blocked = {r.category for r in WASTE_RULES if is_blocked(r)}
    assert blocked, "fixture assumption: the pool has blocked rules"
    assert blocked & _categories("Databricks") == set()
    assert {r.category for r in blocked_rules("Databricks")} == blocked
    # A provider with no blocked patterns of its own must not inherit Databricks'.
    assert blocked_rules("AWS") == ()


def test_cross_provider_rules_reach_a_provider_with_no_rules_of_its_own() -> None:
    """A FOCUS-cost-only provider still gets the four genuinely cross-provider rules, so
    its coverage table says "no data" rather than rendering nothing at all."""
    gcp = _categories("GCP")
    assert gcp == {"underutilized", "idle", "failed", "sql_warehouse_user_concentration"}


def test_unmeasured_entity_type_groups_collapse_out_of_the_table() -> None:
    """S3's storage rule must not occupy a row on a Redshift *cluster*'s coverage table.

    It's evaluable for AWS, so it's in `coverage_groups("AWS")`, but at cluster scope no
    `storage` telemetry is in play. It collapses into the trailing "no telemetry at all"
    line — still stated, because "not evaluated" is not "clean", but not crowding out the
    rules that were actually in play.
    """
    groups = coverage_groups("AWS")
    kept, dry = _split_dry_groups(groups, measured_types={"sql_warehouse", "table"}, fired=set())

    kept_cats = {r.category for _, rules in kept for r in rules}
    dry_cats = {r.category for r in dry}

    assert "s3_intelligent_tiering" in dry_cats
    assert "redshift_table_unused" in kept_cats
    # query_pattern had no telemetry either → collapsed.
    assert "redshift_query_pattern_skew" in dry_cats
    # The cross-provider group ("" entity_type) is always in play.
    assert "idle" in kept_cats


def test_a_fired_rule_survives_the_collapse_even_without_telemetry_rows() -> None:
    """If a rule actually fired, its group stays — a finding must never be collapsed away
    on the grounds that its entity type looked unmeasured."""
    groups = coverage_groups("AWS")
    kept, dry = _split_dry_groups(
        groups, measured_types=set(), fired={"s3_intelligent_tiering"}
    )
    assert "s3_intelligent_tiering" in {r.category for _, rules in kept for r in rules}
    assert "s3_intelligent_tiering" not in {r.category for r in dry}


def test_databricks_gets_a_coverage_table_at_all() -> None:
    """Databricks owns most of the pool and had NO coverage table while the renderer lived
    in the Redshift view — the biggest honesty gap this change closes."""
    rows = rule_coverage_rows(pd.DataFrame(), set(), coverage_groups("Databricks"))
    assert len(rows) >= 20
    # With no telemetry and no findings, every row must read "no data" — never "clean".
    assert {r["Status"] for r in rows} <= {"no data", "clean"}
    # The cross-provider rules have no entity_type to be unmeasured, so they read clean;
    # everything keyed to a specific entity type reads "no data".
    by_cat = {r["category"]: r for r in rows}
    assert by_cat["oversized_nodes"]["Status"] == "no data"
    assert by_cat["idle"]["Status"] == "clean"


def test_status_dot_distinguishes_fired_unpriced_clean_and_no_data() -> None:
    """The dot is the only at-a-glance difference between "checked, nothing found" and
    "never checked", so the four states must render distinctly."""
    fired_css, _ = status_dot_style("fired · 2 entities", "WASTE")
    unpriced_css, _ = status_dot_style("fired · x (unpriced)", "WASTE")
    clean_css, _ = status_dot_style("clean", "WASTE")
    no_data_css, _ = status_dot_style("no data", "WASTE")

    assert len({fired_css, unpriced_css, clean_css, no_data_css}) == 4
    assert "dashed" in no_data_css, "no data must be visually distinct from clean"
    assert "dashed" not in clean_css
