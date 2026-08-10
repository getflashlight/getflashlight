"""End-to-end contract for the schema-driven demo generator."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from flashlight.core.settings import get_settings


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_sample_is_reconciled_and_cleanup_is_scoped(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view, run_select
    from flashlight.lake import paths
    from flashlight.sample import SAMPLE_CONNECTOR, cleanup, load_sample, scenario

    load_sample()

    # The FOCUS parent total equals its resource drill-down, and every scenario
    # entity owner is one of the canonical people defined by the Pydantic schema.
    parent = query_view("databricks.monthly_bill")
    resources = query_view("databricks.resource_month")
    assert sum(row["net_cost"] for row in parent) == sum(row["net_cost"] for row in resources)

    # Snowflake is seeded through the same FOCUS BRONZE writer and is consequently
    # published as an ordinary provider GOLD group, not a dashboard-local fixture.
    snowflake = query_view("snowflake.monthly_bill")
    snowflake_resources = query_view("snowflake.resource_month")
    assert snowflake
    assert sum(row["net_cost"] for row in snowflake) == sum(
        row["net_cost"] for row in snowflake_resources
    )
    snowflake_credits = run_select(
        "SELECT sum(net_cost) AS total FROM snowflake.credits_month "
        "WHERE charge_description = 'Snowflake support credit'"
    )[0]["total"]
    assert float(snowflake_credits) == pytest.approx(-420.0)
    snowflake_efficiency = run_select(
        "SELECT count(*) AS count FROM efficiency.waste_record "
        "WHERE provider_name = 'Snowflake'"
    )[0]["count"]
    snowflake_policy = run_select(
        "SELECT count(*) AS count FROM policy.policy_record "
        "WHERE provider_name = 'Snowflake'"
    )[0]["count"]
    snowflake_drivers = run_select(
        "SELECT count(*) AS count FROM driver_health.driver_health "
        "WHERE provider_name = 'Snowflake'"
    )[0]["count"]
    assert snowflake_efficiency > 0
    assert snowflake_policy > 0
    assert snowflake_drivers > 0
    finance_driver_versions = {
        row["client_driver"]
        for row in run_select(
            "SELECT DISTINCT client_driver FROM driver_health.driver_health "
            "WHERE provider_name = 'Snowflake' "
            "AND cluster_id = 'warehouse:redshift-finance'"
        )
    }
    assert finance_driver_versions == {"PythonConnector 3.6.0", "PythonConnector 3.10.1"}

    # The Home page folds AWS-billed Databricks storage and compute into the Databricks
    # stack, while aws.* intentionally remains Redshift-only.  Assert that every mock
    # cost is represented exactly once in that visible, cross-page accounting model.
    dbx = run_select(
        "SELECT gross_cost FROM databricks.monthly_bill WHERE charge_month = '2026-07-01'"
    )[0]["gross_cost"]
    aws = run_select("SELECT gross_cost FROM aws.monthly_bill WHERE charge_month = '2026-07-01'")[
        0
    ]["gross_cost"]
    storage = run_select(
        "SELECT sum(gross_cost) AS total FROM storage.backing_storage_month "
        "WHERE charge_month = '2026-07-01' AND mapping = 'databricks'"
    )[0]["total"]
    compute = run_select(
        "SELECT sum(gross_cost) AS total FROM compute.backing_compute_month "
        "WHERE charge_month = '2026-07-01' AND mapping = 'databricks'"
    )[0]["total"]
    home_total = float(dbx + aws + storage + compute)
    databricks_total = float(dbx + storage + compute)
    # Databricks uses the observed production-shaped mix: vendor DBUs 76.1%, AWS
    # backing compute 16.8%, and AWS backing storage 7.1%.
    assert databricks_total == pytest.approx(44200.0)
    assert float(dbx) == pytest.approx(databricks_total * 0.761)
    assert float(compute) == pytest.approx(databricks_total * 0.168)
    assert float(storage) == pytest.approx(databricks_total * 0.071)

    # AWS Redshift follows the production subcategory mix: compute 62.1%, managed
    # storage 23.6%, Spectrum 10.6%, concurrency scaling 3.68%, plus its rounding tail.
    redshift_components = {
        row["cost_subcategory"]: float(row["net_cost"])
        for row in run_select(
            "SELECT cost_subcategory, sum(net_cost) AS net_cost "
            "FROM aws.spend_by_cost_subcategory_month "
            "WHERE charge_month = '2026-07-01' GROUP BY cost_subcategory"
        )
    }
    assert float(aws) == pytest.approx(58600.0)
    assert redshift_components == pytest.approx(
        {
            "compute": 36390.6,
            "storage": 13829.6,
            "spectrum_scan": 6211.6,
            "concurrency_scaling": 2156.48,
            "other": 11.72,
        }
    )
    assert home_total == pytest.approx(102800.0)
    assert home_total == pytest.approx(databricks_total + float(aws))

    # The mocked utilization/efficiency queue is a separate, non-additive action
    # layer. The Redshift/Databricks amounts remain the existing 10% demo model;
    # Snowflake adds its independently measured low-utilization warehouse finding.
    opportunities = {
        row["provider_name"]: float(row["recoverable_cost"])
        for row in run_select(
            "SELECT provider_name, sum(recoverable_cost) AS recoverable_cost "
            "FROM efficiency.waste_record WHERE charge_month = '2026-07-01' "
            "GROUP BY provider_name"
        )
    }
    assert opportunities == pytest.approx(
        {"Databricks": 4420.0, "AWS": 5860.0, "Snowflake": 6587.88}
    )
    assert opportunities["Databricks"] + opportunities["AWS"] == pytest.approx(
        home_total * 0.10
    )

    # A daily chart and a date-range control should see a real month-shaped mock bill,
    # not a single synthetic point on the fifteenth.
    dates = run_select(
        "SELECT min(charge_day) AS first_day, max(charge_day) AS last_day "
        "FROM databricks.spend_trend_daily "
        "WHERE charge_day >= '2026-07-01' AND charge_day < '2026-08-01'"
    )[0]
    assert dates["first_day"] == "2026-07-01"
    assert dates["last_day"] == "2026-07-31"
    partial_dates = run_select(
        "SELECT min(charge_day) AS first_day, max(charge_day) AS last_day "
        "FROM databricks.spend_trend_daily "
        "WHERE charge_day >= '2026-08-01' AND charge_day < '2026-09-01'"
    )[0]
    assert partial_dates == {"first_day": "2026-08-01", "last_day": "2026-08-09"}
    assert (
        run_select(
            "SELECT count(DISTINCT net_cost) AS count FROM databricks.spend_trend_daily "
            "WHERE charge_day >= '2026-07-01' AND charge_day < '2026-08-01'"
        )[0]["count"]
        > 20
    )

    # The detailed screens receive truthful, reconcilable source facts rather than a
    # uniform total copied into every row: S3 has its four visible charge families,
    # token allocation equals model-serving spend, and Databricks policy telemetry
    # produces both compliant and non-compliant findings.
    storage_types = {
        row["cost_subcategory"]
        for row in run_select(
            "SELECT DISTINCT cost_subcategory FROM storage.backing_storage_month "
            "WHERE charge_month = '2026-07-01' AND mapping = 'databricks'"
        )
    }
    assert storage_types == {"data_transfer", "other", "requests", "storage"}
    allocated_ai = run_select(
        "SELECT sum(allocated_cost) AS total FROM ai_usage.requester_month "
        "WHERE charge_month = '2026-07-01'"
    )[0]["total"]
    model_serving = run_select(
        "SELECT sum(net_cost) AS total FROM databricks.ai_spend_month "
        "WHERE charge_month = '2026-07-01' AND ai_product_family = 'model_serving'"
    )[0]["total"]
    assert float(allocated_ai) == pytest.approx(float(model_serving))
    policy_statuses = {
        row["status"]
        for row in run_select(
            "SELECT DISTINCT status FROM policy.policy_record "
            "WHERE provider_name = 'Databricks' AND charge_month = '2026-07-01'"
        )
    }
    assert policy_statuses == {"compliant", "non_compliant"}

    # Every visible demo surface gets source-shaped mock data. In particular, the
    # Redshift drill-down has two real clusters, and utilization, owners, policy,
    # driver-health, AI, and backing-cost tabs all have something truthful to render.
    redshift_clusters = run_select(
        "SELECT DISTINCT resource_id FROM aws.resource_month "
        "WHERE charge_month = '2026-07-01' ORDER BY resource_id"
    )
    assert [row["resource_id"] for row in redshift_clusters] == [
        "arn:aws:redshift:us-east-1:123456789012:cluster:finance",
        "arn:aws:redshift:us-east-1:123456789012:cluster:prod-analytics",
    ]
    # Production-shaped detail is present below the headline: a SKU long tail, a
    # broader owner/resource population, five managed storage locations, and the
    # requested two Redshift clusters all have real rows to drill into.
    assert (
        run_select(
            "SELECT count(*) AS count FROM databricks.spend_by_sku_month "
            "WHERE charge_month = '2026-07-01'"
        )[0]["count"]
        >= 15
    )
    assert (
        run_select(
            "SELECT count(DISTINCT resource_id) AS count FROM databricks.resource_month "
            "WHERE charge_month = '2026-07-01'"
        )[0]["count"]
        == 10
    )
    assert (
        run_select(
            "SELECT count(DISTINCT tag_value) AS count FROM databricks.spend_by_tag_month "
            "WHERE charge_month = '2026-07-01' AND tag_key = 'owner'"
        )[0]["count"]
        >= 6
    )
    assert run_select("SELECT count(*) AS count FROM storage.storage_location")[0]["count"] == 5
    assert (
        run_select(
            "SELECT count(*) AS count FROM compute.compute_instance "
            "WHERE charge_month = '2026-07-01'"
        )[0]["count"]
        == 5
    )
    for view in (
        "efficiency.efficiency_entity_month",
        "efficiency.utilization_entity_month",
        "efficiency.waste_by_owner_month",
        "efficiency.waste_record",
        "policy.policy_record",
        "driver_health.driver_health",
        "ai_usage.endpoint_month",
        "storage.backing_storage_month",
        "compute.backing_compute_month",
    ):
        assert query_view(view), f"{view} should have demo rows"
    people = {person.email for person in scenario().people}
    assert {entity.owner_email for entity in scenario().databricks_clusters} <= people

    bronze_part = paths.bronze_dir() / f"x_source_connector={SAMPLE_CONNECTOR}"
    run_files = list(paths.runs_dir().glob(f"*-{SAMPLE_CONNECTOR}.parquet"))
    assert bronze_part.exists()
    assert run_files
    cleanup()
    assert not bronze_part.exists()
    assert not list(paths.runs_dir().glob(f"*-{SAMPLE_CONNECTOR}.parquet"))
    cleanup()  # idempotent


def test_gold_contract_audit_accepts_published_demo(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.sample import _audit_gold_contract, load_sample

    load_sample()
    _audit_gold_contract()
