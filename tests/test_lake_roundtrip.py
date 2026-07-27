"""End-to-end: BRONZE Parquet write → partition-replace → GOLD build → read.

Exercises the riskiest part of the rearchitecture — the DuckDB SQL ports
(json_extract_string, the EKS dynamic-tag logic, the TCO joins) — against a real
in-memory DuckDB over real Parquet, with no database.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from flashlight.core.settings import get_settings
from flashlight.focus.enums import (
    ChargeCategory,
    ComputeClass,
    ProviderName,
    ServiceCategory,
)
from flashlight.focus.model import FocusRecord
from flashlight.ingest.base import IngestWindow

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _rec(
    provider: ProviderName,
    service: str,
    cost: str,
    *,
    tags: dict[str, str] | None = None,
    resource_id: str | None = None,
    compute: ComputeClass = ComputeClass.NOT_APPLICABLE,
) -> FocusRecord:
    amount = Decimal(cost)
    return FocusRecord(
        provider_name=provider,
        billing_account_id="acct",
        billing_period_start=date(2026, 5, 1),
        billing_period_end=date(2026, 5, 31),
        charge_period_start=datetime(2026, 5, 15, tzinfo=UTC),
        charge_period_end=datetime(2026, 5, 15, 1, tzinfo=UTC),
        billed_cost=amount,
        effective_cost=amount,
        list_cost=amount,
        charge_category=ChargeCategory.USAGE,
        service_category=ServiceCategory.COMPUTE,
        service_name=service,
        resource_id=resource_id,
        tags=tags or {},
        x_compute_class=compute,
        x_source_connector="t",
    )


def test_partition_replace_is_idempotent(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import bronze, duck

    records = [_rec(ProviderName.AWS, "AmazonEC2", "10"), _rec(ProviderName.AWS, "AmazonS3", "5")]
    assert bronze.write_window("t", _WINDOW, records, ingest_run_id="r1") == 2
    # Re-ingesting the same window replaces rather than appends.
    assert bronze.write_window("t", _WINDOW, records, ingest_run_id="r2") == 2

    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT count(*) FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == 2


def test_transform_builds_gold_with_tco(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    records = [
        # AWS infra tagged to a Databricks cluster → attributed infra.
        _rec(ProviderName.AWS, "AmazonEC2", "100", tags={"ClusterId": "c1"}, resource_id="i-1"),
        # Untagged AWS → unattributed bucket.
        _rec(ProviderName.AWS, "AmazonS3", "20", resource_id="bkt"),
        # Databricks classic compute → DBU + attributed infra.
        _rec(ProviderName.DATABRICKS, "jobs", "40", resource_id="c1", compute=ComputeClass.CLASSIC),
    ]
    bronze.write_window("t", _WINDOW, records, ingest_run_id="r1")

    published = build_gold()
    assert published > 0

    # GOLD is split per provider on disk: gold/<group>/<view>.parquet, + shared/ TCO.
    from flashlight.lake import paths

    gold = paths.gold_dir()
    assert (gold / "aws" / "monthly_bill.parquet").exists()
    assert (gold / "databricks" / "monthly_bill.parquet").exists()
    assert (gold / "shared" / "tco_summary_month.parquet").exists()

    # Per-provider files are pre-sliced: every row carries that provider.
    aws_bill = query_view("aws.monthly_bill")
    assert aws_bill, "aws.monthly_bill should have rows"
    assert {r["provider_name"] for r in aws_bill} == {"AWS"}
    assert {r["provider_name"] for r in query_view("databricks.monthly_bill")} == {"Databricks"}

    tco = query_view("shared.tco_summary_month")
    assert tco, "tco_summary_month should have rows"
    row = tco[0]
    assert row["dbu_cost"] == pytest.approx(40.0)
    assert row["attributed_infra_cost"] == pytest.approx(100.0)
    assert row["unattributed_infra_cost"] == pytest.approx(20.0)
    assert row["total_cost"] == pytest.approx(160.0)


def test_write_window_accepts_generator_input(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import bronze, duck

    def _records() -> Iterator[FocusRecord]:
        yield _rec(ProviderName.AWS, "AmazonEC2", "10")
        yield _rec(ProviderName.AWS, "AmazonS3", "5")

    assert bronze.write_window("t", _WINDOW, _records(), ingest_run_id="r1") == 2
    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT count(*) FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == 2


def test_write_window_chunks_and_dedupes_across_chunks(  # type: ignore[no-untyped-def]
    lake_home, monkeypatch
) -> None:
    from flashlight.lake import bronze, duck

    monkeypatch.setattr(bronze, "CHUNK_ROWS", 2)

    def _records() -> Iterator[FocusRecord]:
        # 5 records, one truly repeated (identical in every field) across what will
        # be two different chunks under CHUNK_ROWS=2 — the duplicate must still be
        # dropped. A same-resource-different-cost row is NOT a duplicate — it's a
        # distinct charge that happens to share a dimension — so must NOT collapse.
        yield _rec(ProviderName.AWS, "AmazonEC2", "1", resource_id="i-1")
        yield _rec(ProviderName.AWS, "AmazonEC2", "2", resource_id="i-2")
        yield _rec(ProviderName.AWS, "AmazonEC2", "3", resource_id="i-3")
        yield _rec(ProviderName.AWS, "AmazonEC2", "1", resource_id="i-1")  # true dup of i-1
        yield _rec(ProviderName.AWS, "AmazonEC2", "4", resource_id="i-4")

    written = bronze.write_window("t", _WINDOW, _records(), ingest_run_id="r1")
    assert written == 4

    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT count(*) FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == 4


def test_write_window_repurges_on_mid_stream_failure(  # type: ignore[no-untyped-def]
    lake_home,
) -> None:
    from flashlight.lake import bronze, paths

    def _records() -> Iterator[FocusRecord]:
        yield _rec(ProviderName.AWS, "AmazonEC2", "10")
        raise RuntimeError("connector blew up mid-stream")

    with pytest.raises(RuntimeError):
        bronze.write_window("t", _WINDOW, _records(), ingest_run_id="r1")

    connector_dir = paths.bronze_dir() / "x_source_connector=t"
    assert not connector_dir.exists() or not any(connector_dir.iterdir())


def test_tag_explosion(lake_home) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.lake import bronze
    from flashlight.transform.runner import build_gold

    records = [
        _rec(ProviderName.AWS, "AmazonEC2", "30", tags={"team": "data", "env": "prod"}),
    ]
    bronze.write_window("t", _WINDOW, records, ingest_run_id="r1")
    build_gold()

    rows = query_view("aws.spend_by_tag_month")
    pairs = {(r["tag_key"], r["tag_value"]) for r in rows}
    assert ("team", "data") in pairs
    assert ("env", "prod") in pairs
