"""Vectorized FOCUS-CSV seed loader — mapping, coercions, and GOLD round-trip.

No network: writes a tiny FOCUS CSV and runs the same DuckDB path `auralake
sample` uses, asserting the set-based mapping mirrors the connector's rules
(NULL sentinels → None, unknown ServiceCategory → Other, tags exploded).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from auralake.core.settings import get_settings

# One clean row + one with NULL sentinels and an out-of-vocab ServiceCategory.
_CSV = (
    "ProviderName,BillingAccountId,BillingCurrency,BillingPeriodStart,BillingPeriodEnd,"
    "ChargePeriodStart,ChargePeriodEnd,ChargeCategory,ChargeClass,ServiceCategory,"
    "ServiceName,SkuId,ResourceId,EffectiveCost,ListCost,ConsumedQuantity,ConsumedUnit,Tags\n"
    "AWS,acct,USD,2024-09-01,2024-09-30,2024-09-15 00:00:00,2024-09-15 01:00:00,Usage,NULL,"
    'Compute,AmazonEC2,sku1,i-1,10.5,12,3,Hrs,"{""team"": ""data""}"\n'
    "Microsoft,acct,USD,2024-09-01,2024-09-30,2024-09-16 00:00:00,2024-09-16 01:00:00,Usage,"
    "NULL,Bogus,VMs,NULL,NULL,5,5,NULL,NULL,NULL\n"
)


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("AURALAKE_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_seed_maps_and_builds_gold(lake_home, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from auralake.gold.reader import query_view
    from auralake.lake import seed
    from auralake.transform.runner import build_gold

    csv_path = tmp_path / "focus.csv"
    csv_path.write_text(_CSV)

    count = seed.seed_from_csv(csv_path, connector="t", ingest_run_id="r1")
    assert count == 2

    build_gold()

    bill = {r["provider_name"] for r in query_view("gold.monthly_bill")}
    assert {"AWS", "Microsoft"} <= bill

    # Out-of-vocab ServiceCategory ("Bogus") coerces to "Other", mirroring _coerce.
    services = query_view("gold.spend_by_service_month")
    microsoft = [r for r in services if r["provider_name"] == "Microsoft"]
    assert microsoft and microsoft[0]["service_category"] == "Other"

    # Tags JSON exploded set-based.
    tags = {(r["tag_key"], r["tag_value"]) for r in query_view("gold.spend_by_tag_month")}
    assert ("team", "data") in tags


def test_seed_rejects_mixed_currency(lake_home, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from auralake.core.exceptions import FocusValidationError
    from auralake.lake import seed

    csv_path = tmp_path / "eur.csv"
    csv_path.write_text(
        "ProviderName,BillingAccountId,BillingCurrency,ChargePeriodStart,ChargePeriodEnd,"
        "ChargeCategory,ServiceCategory,ServiceName,EffectiveCost\n"
        "AWS,acct,EUR,2024-09-15 00:00:00,2024-09-15 01:00:00,Usage,Compute,AmazonEC2,10\n"
    )
    with pytest.raises(FocusValidationError):
        seed.seed_from_csv(csv_path, connector="t", ingest_run_id="r1")
