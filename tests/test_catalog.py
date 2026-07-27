"""GOLD catalog: group-aware names, provider slugging, and data-driven expansion."""

from __future__ import annotations

from flashlight.transform.catalog import (
    DRIVER_HEALTH_BASE_VIEWS,
    EFFICIENCY_BASE_VIEWS,
    PROVIDER_BASE_VIEWS,
    SHARED_BASE_VIEWS,
    build_catalog,
    provider_group,
)


def test_provider_group_slugs() -> None:
    assert provider_group("AWS") == "aws"
    assert provider_group("Databricks") == "databricks"
    assert provider_group("Microsoft") == "microsoft"
    assert provider_group("Google Cloud") == "google_cloud"


def test_build_catalog_expands_per_group() -> None:
    cat = build_catalog(["aws", "databricks"])
    # provider-scoped views per provider + the fixed shared/efficiency/driver_health groups.
    assert len(cat) == (
        len(PROVIDER_BASE_VIEWS) * 2
        + len(SHARED_BASE_VIEWS)
        + len(EFFICIENCY_BASE_VIEWS)
        + len(DRIVER_HEALTH_BASE_VIEWS)
    )

    names = {v.name for v in cat}
    assert "aws.monthly_bill" in names
    assert "databricks.monthly_bill" in names
    assert "shared.tco_summary_month" in names
    assert "efficiency.waste_record" in names
    assert "driver_health.driver_health" in names
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
        {f"shared.{s.view}" for s in SHARED_BASE_VIEWS}
        | {f"efficiency.{s.view}" for s in EFFICIENCY_BASE_VIEWS}
        | {f"driver_health.{s.view}" for s in DRIVER_HEALTH_BASE_VIEWS}
    )
    assert {v.name for v in cat} == expected
