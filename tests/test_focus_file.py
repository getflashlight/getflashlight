"""focus_file connector: vectorized ingest() over a local FOCUS CSV/Parquet.

No network. Exercises the same DuckDB mapping path (flashlight.focus.sql_mapping)
as aws_focus/seed, end to end: real file on disk -> real Parquet in BRONZE -> GOLD.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from flashlight.core.exceptions import ConnectorError, FocusValidationError
from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.ingest.config import FocusFileConfig
from flashlight.ingest.connectors.focus_file import FocusFileConnector

_CSV = (
    "ProviderName,BillingAccountId,BillingCurrency,BillingPeriodStart,BillingPeriodEnd,"
    "ChargePeriodStart,ChargePeriodEnd,ChargeCategory,ChargeClass,ServiceCategory,"
    "ServiceName,SkuId,ResourceId,EffectiveCost,ListCost,ConsumedQuantity,ConsumedUnit,Tags\n"
    "AWS,acct,USD,2024-09-01,2024-09-30,2024-09-15 00:00:00,2024-09-15 01:00:00,Usage,NULL,"
    'Compute,AmazonEC2,sku1,i-1,10.5,12,3,Hrs,"{""team"": ""data""}"\n'
    "Microsoft,acct,USD,2024-09-01,2024-09-30,2024-09-16 00:00:00,2024-09-16 01:00:00,Usage,"
    "NULL,Bogus,VMs,NULL,NULL,5,5,NULL,NULL,NULL\n"
)
_WINDOW = IngestWindow(date(2024, 9, 1), date(2024, 9, 30))


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_missing_file_raises(lake_home) -> None:  # type: ignore[no-untyped-def]
    connector = FocusFileConnector(FocusFileConfig(path="/nope/does-not-exist.csv"))
    with pytest.raises(ConnectorError):
        connector.ingest(_WINDOW, run_id="r1")


def test_ingest_csv_builds_bronze_and_gold(lake_home, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from flashlight.gold.reader import query_view
    from flashlight.transform.runner import build_gold

    csv_path = tmp_path / "focus.csv"
    csv_path.write_text(_CSV)
    connector = FocusFileConnector(FocusFileConfig(path=str(csv_path)))

    written = connector.ingest(_WINDOW, run_id="r1")
    assert written == 2

    build_gold()
    assert {r["provider_name"] for r in query_view("aws.monthly_bill")} == {"AWS"}
    assert {r["provider_name"] for r in query_view("microsoft.monthly_bill")} == {"Microsoft"}

    # Out-of-vocab ServiceCategory ("Bogus") coerces to "Other".
    services = query_view("microsoft.spend_by_service_month")
    assert services and services[0]["service_category"] == "Other"

    # Tags exploded (the team tag is on the AWS row).
    tags = {(r["tag_key"], r["tag_value"]) for r in query_view("aws.spend_by_tag_month")}
    assert ("team", "data") in tags


def test_ingest_parquet(lake_home, tmp_path) -> None:  # type: ignore[no-untyped-def]
    table = pa.table(
        {
            "ProviderName": ["AWS"],
            "BillingAccountId": ["acct"],
            "BillingCurrency": ["USD"],
            "ChargePeriodStart": [date(2024, 9, 15)],
            "ChargePeriodEnd": [date(2024, 9, 15)],
            "ChargeCategory": ["Usage"],
            "ServiceCategory": ["Compute"],
            "ServiceName": ["AmazonEC2"],
            "EffectiveCost": [10.0],
        }
    )
    path = tmp_path / "focus.parquet"
    pq.write_table(table, path)  # type: ignore[no-untyped-call]
    connector = FocusFileConnector(FocusFileConfig(path=str(path)))

    written = connector.ingest(_WINDOW, run_id="r1")
    assert written == 1


def test_respect_window_drops_out_of_window_rows(lake_home, tmp_path) -> None:  # type: ignore[no-untyped-def]
    csv_path = tmp_path / "focus.csv"
    csv_path.write_text(
        "ProviderName,BillingAccountId,BillingCurrency,BillingPeriodStart,BillingPeriodEnd,"
        "ChargePeriodStart,ChargePeriodEnd,ChargeCategory,ServiceCategory,ServiceName,"
        "EffectiveCost\n"
        "AWS,acct,USD,2024-09-01,2024-09-30,2024-09-15 00:00:00,2024-09-15 01:00:00,Usage,"
        "Compute,AmazonEC2,10\n"
        "AWS,acct,USD,2023-01-01,2023-01-31,2023-01-15 00:00:00,2023-01-15 01:00:00,Usage,"
        "Compute,AmazonEC2,20\n"
    )
    connector = FocusFileConnector(FocusFileConfig(path=str(csv_path), respect_window=True))
    assert connector.ingest(_WINDOW, run_id="r1") == 1

    connector_all = FocusFileConnector(
        FocusFileConfig(path=str(csv_path), respect_window=False)
    )
    # A fresh window covering both rows' months, since write_window_sql purges by window.
    wide_window = IngestWindow(date(2023, 1, 1), date(2024, 9, 30))
    assert connector_all.ingest(wide_window, run_id="r2") == 2


def test_ingest_rejects_mixed_currency(lake_home, tmp_path) -> None:  # type: ignore[no-untyped-def]
    csv_path = tmp_path / "eur.csv"
    csv_path.write_text(
        "ProviderName,BillingAccountId,BillingCurrency,ChargePeriodStart,ChargePeriodEnd,"
        "ChargeCategory,ServiceCategory,ServiceName,EffectiveCost\n"
        "AWS,acct,EUR,2024-09-15 00:00:00,2024-09-15 01:00:00,Usage,Compute,AmazonEC2,10\n"
    )
    connector = FocusFileConnector(FocusFileConfig(path=str(csv_path)))
    with pytest.raises(FocusValidationError):
        connector.ingest(_WINDOW, run_id="r1")
