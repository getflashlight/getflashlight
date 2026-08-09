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

    # The Home page folds AWS-billed Databricks storage and compute into the Databricks
    # stack, while aws.* intentionally remains Redshift-only.  Assert that every mock
    # cost is represented exactly once in that visible, cross-page accounting model.
    dbx = run_select(
        "SELECT gross_cost FROM databricks.monthly_bill WHERE charge_month = '2026-07-01'"
    )[0]["gross_cost"]
    aws = run_select(
        "SELECT gross_cost FROM aws.monthly_bill WHERE charge_month = '2026-07-01'"
    )[0]["gross_cost"]
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
    # Databricks is a fully additive 65% DBU / 30% EC2 / 5% S3 model, and the
    # Home page's provider row is its total rather than an unrelated second number.
    assert databricks_total == pytest.approx(44200.0)
    assert float(dbx) == pytest.approx(databricks_total * 0.65)
    assert float(compute) == pytest.approx(databricks_total * 0.30)
    assert float(storage) == pytest.approx(databricks_total * 0.05)

    # AWS Redshift is likewise one additive provider total: 65% cluster compute,
    # 20% managed storage, 10% concurrency scaling, and 5% Spectrum.
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
            "compute": 38090.0,
            "storage": 11720.0,
            "concurrency_scaling": 5860.0,
            "spectrum": 2930.0,
        }
    )
    assert home_total == pytest.approx(102800.0)
    assert home_total == pytest.approx(databricks_total + float(aws))

    # The mocked utilization/efficiency queue is a separate, non-additive action
    # layer: its priced opportunities are exactly 10% of each all-in platform total.
    opportunities = {
        row["provider_name"]: float(row["recoverable_cost"])
        for row in run_select(
            "SELECT provider_name, sum(recoverable_cost) AS recoverable_cost "
            "FROM efficiency.waste_record WHERE charge_month = '2026-07-01' "
            "GROUP BY provider_name"
        )
    }
    assert opportunities == pytest.approx({"Databricks": 4420.0, "AWS": 5860.0})
    assert sum(opportunities.values()) == pytest.approx(home_total * 0.10)

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

    # Every visible demo surface gets source-shaped mock data. In particular, the
    # Redshift drill-down has two real clusters, and utilization, owners, policy,
    # driver-health, AI, and backing-cost tabs all have something truthful to render.
    redshift_clusters = run_select(
        "SELECT DISTINCT resource_id FROM aws.resource_month "
        "WHERE charge_month = '2026-07-01' ORDER BY resource_id"
    )
    assert [row["resource_id"] for row in redshift_clusters] == [
        "redshift-finance",
        "redshift-prod-analytics",
    ]
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
