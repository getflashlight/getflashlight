"""AwsFocusConnector.ingest(): the vectorized bulk path, end to end.

No real S3 — `_manifest_files` is monkeypatched to return a local Parquet path
directly (DuckDB's `read_parquet` doesn't care whether the path has an `s3://`
scheme or not), so this exercises the real mapping/write path (DuckDB scan ->
flashlight.focus.sql_mapping -> bronze.write_window_sql) against a real file.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from unittest.mock import MagicMock

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from flashlight.core.exceptions import FocusValidationError
from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.ingest.config import AwsFocusConfig
from flashlight.ingest.connectors.aws_focus import AwsFocusConnector

_WINDOW = IngestWindow(date(2026, 6, 1), date(2026, 6, 30))


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _connector(monkeypatch, tmp_path, files: list[str], **config_kw: object):  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "flashlight.ingest.connectors.aws_focus.aws_client", MagicMock(return_value=MagicMock())
    )
    config = AwsFocusConfig.model_validate({"s3_bucket": "b", "region": "us-west-2", **config_kw})
    connector = AwsFocusConnector(config)
    monkeypatch.setattr(connector, "_manifest_files", lambda window: files)
    return connector


def _write_parquet(path, rows: list[dict[str, object]]) -> str:  # type: ignore[no-untyped-def]
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, path)  # type: ignore[no-untyped-call]
    return str(path)


def test_ingest_maps_and_writes_bronze(lake_home, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.transform.runner import build_gold

    path = _write_parquet(
        tmp_path / "focus.parquet",
        [
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "BillingPeriodStart": date(2026, 6, 1),
                "BillingPeriodEnd": date(2026, 6, 30),
                "ChargePeriodStart": date(2026, 6, 15),
                "ChargePeriodEnd": date(2026, 6, 15),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Compute",
                "ServiceName": "AmazonEC2",
                "EffectiveCost": 10.0,
                "BilledCost": 10.0,
                "ResourceId": "i-1",
            },
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "BillingPeriodStart": date(2026, 6, 1),
                "BillingPeriodEnd": date(2026, 6, 30),
                "ChargePeriodStart": date(2026, 6, 16),
                "ChargePeriodEnd": date(2026, 6, 16),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Storage",
                "ServiceName": "AmazonS3",
                "EffectiveCost": 5.0,
                "BilledCost": 5.0,
                "ResourceId": "bkt",
            },
        ],
    )
    # Explicit [] — tests the general any-service mapping path, independent of
    # AwsFocusConfig.include_services's Redshift-only default.
    connector = _connector(monkeypatch, tmp_path, [path], include_services=[])

    written = connector.ingest(_WINDOW, run_id="r1")
    assert written == 2

    build_gold()
    rows = query_view("aws.monthly_bill")
    assert rows and rows[0]["net_cost"] == pytest.approx(15.0)


def test_ingest_stamps_focus_version_1_2(lake_home, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import duck

    path = _write_parquet(
        tmp_path / "focus.parquet",
        [
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 15),
                "ChargePeriodEnd": date(2026, 6, 15),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Compute",
                "ServiceName": "AmazonEC2",
                "EffectiveCost": 10.0,
            },
        ],
    )
    connector = _connector(monkeypatch, tmp_path, [path], include_services=[])
    connector.ingest(_WINDOW, run_id="r1")

    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT x_focus_version FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == "1.2"


def test_ingest_classifies_redshift_subcategory(lake_home, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.lake import duck

    path = _write_parquet(
        tmp_path / "focus.parquet",
        [
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 15),
                "ChargePeriodEnd": date(2026, 6, 15),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Databases",
                "ServiceName": "Amazon Redshift",
                "ChargeDescription": "Concurrency Scaling usage",
                "EffectiveCost": 10.0,
            },
        ],
    )
    connector = _connector(monkeypatch, tmp_path, [path])
    connector.ingest(_WINDOW, run_id="r1")

    con = duck.connect()
    duck.register_bronze(con)
    row = con.execute("SELECT x_cost_subcategory FROM raw.focus_record").fetchone()
    assert row is not None
    assert row[0] == "concurrency_scaling"


def test_ingest_no_manifests_returns_zero(lake_home, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    connector = _connector(monkeypatch, tmp_path, [])
    assert connector.ingest(_WINDOW, run_id="r1") == 0


def test_ingest_rejects_mixed_currency(lake_home, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = _write_parquet(
        tmp_path / "focus.parquet",
        [
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 15),
                "ChargePeriodEnd": date(2026, 6, 15),
                "BillingCurrency": "EUR",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Compute",
                "ServiceName": "AmazonEC2",
                "EffectiveCost": 10.0,
            },
        ],
    )
    connector = _connector(monkeypatch, tmp_path, [path], include_services=[])
    with pytest.raises(FocusValidationError):
        connector.ingest(_WINDOW, run_id="r1")


def test_fetch_efficiency_reads_own_bronze_not_s3(lake_home, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """fetch_efficiency must read the BRONZE rows ingest() just wrote, not re-hit S3 —
    _manifest_files is monkeypatched to explode if called from fetch_efficiency."""
    path = _write_parquet(
        tmp_path / "focus.parquet",
        [
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 10),
                "ChargePeriodEnd": date(2026, 6, 10),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Storage",
                "ServiceName": "Amazon Simple Storage Service",
                "ChargeDescription": "TimedStorage-Intelligent-Tiering-ByteHrs",
                "ResourceId": "tiered-bucket",
                "EffectiveCost": 3.0,
                "BilledCost": 3.0,
            },
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 20),
                "ChargePeriodEnd": date(2026, 6, 20),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Storage",
                "ServiceName": "Amazon Simple Storage Service",
                "ChargeDescription": "TimedStorage-ByteHrs",
                "ResourceId": "standard-bucket",
                "EffectiveCost": 4.0,
                "BilledCost": 4.0,
            },
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 15),
                "ChargePeriodEnd": date(2026, 6, 15),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Compute",
                "ServiceName": "AmazonEC2",
                "ResourceId": "i-1",
                "EffectiveCost": 10.0,
                "BilledCost": 10.0,
            },
        ],
    )
    connector = _connector(monkeypatch, tmp_path, [path], include_services=[])
    assert connector.ingest(_WINDOW, run_id="r1") == 3

    monkeypatch.setattr(
        connector,
        "_manifest_files",
        lambda window: (_ for _ in ()).throw(AssertionError("must not re-list S3")),
    )

    records = {r.entity_id: r for r in connector.fetch_efficiency(_WINDOW)}
    assert set(records) == {"tiered-bucket", "standard-bucket"}
    assert records["tiered-bucket"].cause_detail["storage_class"] == "intelligent_tiering"
    assert records["standard-bucket"].cause_detail["storage_class"] == "standard"
    assert records["tiered-bucket"].billed_cost == pytest.approx(3.0)
    assert records["tiered-bucket"].charge_month == date(2026, 6, 1)


def test_ingest_pushes_down_service_allow_list(lake_home, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    path = _write_parquet(
        tmp_path / "focus.parquet",
        [
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 15),
                "ChargePeriodEnd": date(2026, 6, 15),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Compute",
                "ServiceName": "AmazonEC2",
                "EffectiveCost": 10.0,
            },
            {
                "ProviderName": "AWS",
                "BillingAccountId": "acct-1",
                "ChargePeriodStart": date(2026, 6, 16),
                "ChargePeriodEnd": date(2026, 6, 16),
                "BillingCurrency": "USD",
                "ChargeCategory": "Usage",
                "ServiceCategory": "Storage",
                "ServiceName": "AmazonS3",
                "EffectiveCost": 5.0,
            },
        ],
    )
    connector = _connector(monkeypatch, tmp_path, [path], include_services=["AmazonS3"])
    written = connector.ingest(_WINDOW, run_id="r1")
    assert written == 1
