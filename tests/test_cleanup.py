"""``flashlight cleanup --connector`` — scoped removal of one connector's BRONZE
data, leaving every other connector (and GOLD's other providers) untouched."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from flashlight.core.settings import get_settings
from flashlight.focus.enums import ChargeCategory, ComputeClass, ProviderName, ServiceCategory
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow


@pytest.fixture
def lake_home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(provider: ProviderName, connector: str) -> FocusRecord:
    when = datetime(2026, 6, 1, tzinfo=UTC)
    return FocusRecord(
        provider_name=provider,
        billing_account_id="acct",
        billing_period_start=date(2026, 6, 1),
        billing_period_end=date(2026, 6, 30),
        charge_period_start=when,
        charge_period_end=when,
        billed_cost=Decimal("10"),
        effective_cost=Decimal("10"),
        list_cost=Decimal("12"),
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name="AmazonEC2",
        tags={},
        x_compute_class=ComputeClass.NOT_APPLICABLE,
        x_source_connector=connector,
    )


def test_purge_connector_removes_only_that_connectors_bronze_and_run_log(  # type: ignore[no-untyped-def]
    lake_home,
) -> None:
    from flashlight.lake import bronze, cleanup, paths, runlog

    window = IngestWindow(date(2026, 6, 1), date(2026, 6, 30))
    bronze.write_window(
        "aws_focus", window, [_rec(ProviderName.AWS, "aws_focus")], ingest_run_id="r-aws"
    )
    bronze.write_window(
        "databricks",
        window,
        [_rec(ProviderName.DATABRICKS, "databricks")],
        ingest_run_id="r-db",
    )
    now = datetime(2026, 6, 1, tzinfo=UTC)
    runlog.record_run(
        run_id="r-aws", connector="aws_focus", status="success", rows=1,
        started_at=now, finished_at=now,
    )
    runlog.record_run(
        run_id="r-db", connector="databricks", status="success", rows=1,
        started_at=now, finished_at=now,
    )

    targets = cleanup.connector_targets("aws_focus")
    assert any("x_source_connector=aws_focus" in str(p) for p in targets)
    assert not any("databricks" in str(p) for p in targets)

    removed = cleanup.purge_connector("aws_focus")
    assert removed > 0

    assert not (paths.bronze_dir() / "x_source_connector=aws_focus").exists()
    assert (paths.bronze_dir() / "x_source_connector=databricks").exists()
    assert not list(paths.runs_dir().glob("*-aws_focus.parquet"))
    assert list(paths.runs_dir().glob("*-databricks.parquet"))


def test_purge_connector_is_a_noop_when_nothing_to_remove(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import cleanup, paths

    paths.ensure_layout()
    assert cleanup.connector_targets("aws_focus") == []
    assert cleanup.purge_connector("aws_focus") == 0
