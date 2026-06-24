from dataclasses import dataclass

from auralake.focus.enums import ComputeClass, ServiceCategory
from auralake.ingest.connectors._focus_map import map_focus_row
from auralake.ingest.connectors.databricks import (
    _warehouse_sort_key,
    compute_class_for_sku,
)


@dataclass
class _FakeWarehouse:
    id: str
    name: str | None = None
    cluster_size: str | None = None
    enable_serverless_compute: bool | None = None


def _pick(*warehouses: _FakeWarehouse) -> str:
    return min(warehouses, key=_warehouse_sort_key).id


def test_warehouse_autopick_prefers_smallest_size() -> None:
    chosen = _pick(
        _FakeWarehouse(id="lg", cluster_size="Large"),
        _FakeWarehouse(id="sm", cluster_size="Small"),
        _FakeWarehouse(id="md", cluster_size="Medium"),
        _FakeWarehouse(id="xs", cluster_size="2X-Small"),
    )
    assert chosen == "xs"


def test_warehouse_autopick_serverless_breaks_size_tie() -> None:
    # Same size → serverless wins (no idle infra cost).
    chosen = _pick(
        _FakeWarehouse(id="classic", cluster_size="Small", enable_serverless_compute=False),
        _FakeWarehouse(id="serverless", cluster_size="Small", enable_serverless_compute=True),
    )
    assert chosen == "serverless"


def test_warehouse_autopick_unknown_size_sorts_last() -> None:
    chosen = _pick(
        _FakeWarehouse(id="unknown", cluster_size=None),
        _FakeWarehouse(id="big", cluster_size="4X-Large"),
    )
    assert chosen == "big"


def test_warehouse_autopick_name_is_final_tiebreak() -> None:
    # Identical size + serverless → deterministic by name.
    chosen = _pick(
        _FakeWarehouse(id="b", name="b-wh", cluster_size="Small", enable_serverless_compute=True),
        _FakeWarehouse(id="a", name="a-wh", cluster_size="Small", enable_serverless_compute=True),
    )
    assert chosen == "a"


def test_compute_class_from_sku() -> None:
    assert compute_class_for_sku("ENTERPRISE_JOBS_SERVERLESS_COMPUTE") == ComputeClass.SERVERLESS
    assert compute_class_for_sku("STANDARD_ALL_PURPOSE_COMPUTE") == ComputeClass.CLASSIC
    assert compute_class_for_sku(None) == ComputeClass.CLASSIC


def test_maps_databricks_focus_1_3_output_row() -> None:
    # Shape mirrors the vendored query's output columns.
    row = {
        "ProviderName": "Databricks",
        "BillingAccountId": "acct-123",
        "SubAccountId": "ws-9",
        "SubAccountName": "analytics-ws",
        "BillingPeriodStart": "2026-06-01",
        "BillingPeriodEnd": "2026-07-01",
        "ChargePeriodStart": "2026-06-15 10:00:00",
        "ChargePeriodEnd": "2026-06-15 11:00:00",
        "BillingCurrency": "USD",
        "BilledCost": "12.5",
        "EffectiveCost": "12.5",
        "ListCost": "20.0",
        "ContractedCost": "12.5",
        "ChargeCategory": "Usage",
        "ServiceCategory": "Analytics",
        "ServiceName": "ALL_PURPOSE",
        "SkuId": "STANDARD_ALL_PURPOSE_COMPUTE",
        "ResourceId": "0617-cluster-abc",
        "ConsumedQuantity": "3.0",
        "ConsumedUnit": "DBU",
        "Tags": {"team": "data-eng"},
    }
    rec = map_focus_row(row, "databricks")
    assert rec is not None
    assert rec.provider_name == "Databricks"
    assert rec.service_category == ServiceCategory.ANALYTICS
    assert rec.resource_id == "0617-cluster-abc"  # cluster id → joins to AWS ClusterId tag
    assert str(rec.effective_cost) == "12.5"
    assert rec.tags == {"team": "data-eng"}
    assert compute_class_for_sku(rec.sku_id) == ComputeClass.CLASSIC
