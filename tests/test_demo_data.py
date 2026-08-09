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
    from flashlight.gold.reader import query_view
    from flashlight.lake import paths
    from flashlight.sample import SAMPLE_CONNECTOR, cleanup, load_sample, scenario

    load_sample()

    # The FOCUS parent total equals its resource drill-down, and every scenario
    # entity owner is one of the canonical people defined by the Pydantic schema.
    parent = query_view("databricks.monthly_bill")
    resources = query_view("databricks.resource_month")
    assert sum(row["net_cost"] for row in parent) == sum(row["net_cost"] for row in resources)
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
