"""GOLD catalog: group-aware names, provider slugging, and data-driven expansion."""

from __future__ import annotations

from flashlight.transform.catalog import (
    AI_USAGE_BASE_VIEWS,
    COMPUTE_BASE_VIEWS,
    DRIVER_HEALTH_BASE_VIEWS,
    EFFICIENCY_BASE_VIEWS,
    MEASURE_UNITS,
    PERIOD_DIMENSIONS,
    POLICY_BASE_VIEWS,
    PROVIDER_BASE_VIEWS,
    STORAGE_BASE_VIEWS,
    build_catalog,
    provider_group,
)

_ALL_SPECS = (
    *PROVIDER_BASE_VIEWS,
    *EFFICIENCY_BASE_VIEWS,
    *DRIVER_HEALTH_BASE_VIEWS,
    *POLICY_BASE_VIEWS,
    *AI_USAGE_BASE_VIEWS,
    *STORAGE_BASE_VIEWS,
    *COMPUTE_BASE_VIEWS,
)


def test_every_measure_declares_a_unit() -> None:
    """A consumer formats a figure from its declared unit, so an unclassified
    measure would be rendered by a fallback guess — caught here instead."""
    declared = {measure for spec in _ALL_SPECS for measure in spec.measures}
    assert declared - set(MEASURE_UNITS) == set(), "measures missing from MEASURE_UNITS"
    # And nothing stale: a removed measure left behind would keep asserting a unit
    # for a column no view returns.
    assert set(MEASURE_UNITS) - declared == set(), "MEASURE_UNITS names no view declares"


def test_period_dimensions_are_real_dimensions_and_exclude_lookalikes() -> None:
    """``first_seen_month``/``last_seen_month``/``resolved_month`` all end in "month"
    but are entity attributes, not the charge period a trend runs along."""
    declared = {dimension for spec in _ALL_SPECS for dimension in spec.dimensions}
    assert PERIOD_DIMENSIONS <= declared
    for lookalike in ("first_seen_month", "last_seen_month", "resolved_month"):
        assert lookalike in declared  # still a real dimension...
        assert lookalike not in PERIOD_DIMENSIONS  # ...just not the period


def test_provider_group_slugs() -> None:
    assert provider_group("AWS") == "aws"
    assert provider_group("Databricks") == "databricks"
    assert provider_group("Microsoft") == "microsoft"
    assert provider_group("Google Cloud") == "google_cloud"


def test_build_catalog_expands_per_group() -> None:
    cat = build_catalog(["aws", "databricks"])
    # provider-scoped views per provider + the fixed efficiency/driver_health/policy/
    # ai_usage groups.
    assert len(cat) == (
        len(PROVIDER_BASE_VIEWS) * 2
        + len(EFFICIENCY_BASE_VIEWS)
        + len(DRIVER_HEALTH_BASE_VIEWS)
        + len(POLICY_BASE_VIEWS)
        + len(AI_USAGE_BASE_VIEWS)
        + len(STORAGE_BASE_VIEWS)
        + len(COMPUTE_BASE_VIEWS)
    )

    names = {v.name for v in cat}
    assert "aws.monthly_bill" in names
    assert "databricks.monthly_bill" in names
    assert "efficiency.waste_record" in names
    assert "driver_health.driver_health" in names
    assert "policy.policy_record" in names
    assert "ai_usage.project_month" in names
    assert "storage.backing_storage_month" in names
    assert "compute.backing_compute_month" in names
    assert "aws.ai_spend_month" in names
    assert "aws.commitment_summary_month" in names
    assert "aws.invoice_reconciliation_month" in names
    # No flat `gold.` names remain.
    assert not any(n.startswith("gold.") for n in names)


def test_name_property_and_relpath() -> None:
    cat = build_catalog(["aws"])
    bill = next(v for v in cat if v.view == "monthly_bill")
    assert bill.name == "aws.monthly_bill"
    assert bill.relpath == "aws/monthly_bill.parquet"


def test_catalog_by_name_keys_unique() -> None:
    cat = build_catalog(["aws", "databricks", "microsoft"])
    by_name = {v.name: v for v in cat}
    assert len(by_name) == len(cat)  # no collisions across groups


def test_empty_provider_set_still_has_fixed_groups() -> None:
    cat = build_catalog([])
    expected = (
        {f"efficiency.{s.view}" for s in EFFICIENCY_BASE_VIEWS}
        | {f"driver_health.{s.view}" for s in DRIVER_HEALTH_BASE_VIEWS}
        | {f"policy.{s.view}" for s in POLICY_BASE_VIEWS}
        | {f"ai_usage.{s.view}" for s in AI_USAGE_BASE_VIEWS}
        | {f"storage.{s.view}" for s in STORAGE_BASE_VIEWS}
        | {f"compute.{s.view}" for s in COMPUTE_BASE_VIEWS}
    )
    assert {v.name for v in cat} == expected


def test_fixed_groups_covers_every_non_provider_group() -> None:
    """Every fixed group's constant must be in ``FIXED_GROUPS``.

    This is the phantom-provider guard. ``discover_provider_groups()`` treats any
    ``gold/<dir>`` not in ``FIXED_GROUPS`` as a provider, and the router feeds that
    straight to ``provider_focus.render(group, …)``, which queries
    ``<group>.monthly_bill``. A fixed group left out therefore gets a nav entry, a route
    and a crash — so the set is asserted against the group constants themselves rather
    than a hand-copied literal.
    """
    from flashlight.transform.catalog import (
        AI_USAGE_GROUP,
        COMPUTE_GROUP,
        DRIVER_HEALTH_GROUP,
        EFFICIENCY_GROUP,
        FIXED_GROUPS,
        POLICY_GROUP,
        STORAGE_GROUP,
    )

    assert FIXED_GROUPS == {
        EFFICIENCY_GROUP,
        DRIVER_HEALTH_GROUP,
        POLICY_GROUP,
        AI_USAGE_GROUP,
        STORAGE_GROUP,
        COMPUTE_GROUP,
    }


def test_storage_group_is_not_discovered_as_a_provider(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A published ``gold/storage/`` must not become a provider page.

    The concrete failure this prevents: `/storage` in the nav, rendering the shared
    provider page against a ``storage.monthly_bill`` that does not and will never exist.
    """
    from flashlight.transform.catalog import discover_provider_groups

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    gold = tmp_path / "gold"
    for group in ("aws", "databricks", "storage", "compute", "efficiency", "ai_usage"):
        (gold / group).mkdir(parents=True)
        (gold / group / "some_view.parquet").touch()

    assert discover_provider_groups() == ["aws", "databricks"]
