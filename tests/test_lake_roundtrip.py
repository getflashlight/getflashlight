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

from auralake.core.settings import get_settings
from auralake.focus.enums import (
    ChargeCategory,
    ComputeClass,
    ProviderName,
    ServiceCategory,
)
from auralake.focus.model import FocusRecord
from auralake.ingest.base import IngestWindow

_WINDOW = IngestWindow(date(2026, 5, 1), date(2026, 5, 31))


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AURALAKE_HOME", str(tmp_path))
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
    from auralake.lake import bronze, duck

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
    from auralake.gold.reader import query_view
    from auralake.lake import bronze
    from auralake.transform.runner import build_gold

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

    bill = query_view("gold.monthly_bill")
    assert bill, "monthly_bill should have rows"

    tco = query_view("gold.tco_summary_month")
    assert tco, "tco_summary_month should have rows"
    row = tco[0]
    assert row["dbu_cost"] == pytest.approx(40.0)
    assert row["attributed_infra_cost"] == pytest.approx(100.0)
    assert row["unattributed_infra_cost"] == pytest.approx(20.0)
    assert row["total_cost"] == pytest.approx(160.0)


def test_tag_explosion(lake_home) -> None:  # type: ignore[no-untyped-def]
    from auralake.gold.reader import query_view
    from auralake.lake import bronze
    from auralake.transform.runner import build_gold

    records = [
        _rec(ProviderName.AWS, "AmazonEC2", "30", tags={"team": "data", "env": "prod"}),
    ]
    bronze.write_window("t", _WINDOW, records, ingest_run_id="r1")
    build_gold()

    rows = query_view("gold.spend_by_tag_month")
    pairs = {(r["tag_key"], r["tag_value"]) for r in rows}
    assert ("team", "data") in pairs
    assert ("env", "prod") in pairs
