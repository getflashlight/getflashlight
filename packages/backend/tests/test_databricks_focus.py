from auralake.focus.enums import ComputeClass, ServiceCategory
from auralake.ingest.connectors._focus_map import map_focus_row
from auralake.ingest.connectors.databricks import compute_class_for_sku


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
