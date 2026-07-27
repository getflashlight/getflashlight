from dataclasses import dataclass

import duckdb

from flashlight.focus import sql_mapping
from flashlight.focus.enums import ComputeClass, ServiceCategory
from flashlight.ingest.connectors._focus_map import map_focus_row
from flashlight.ingest.connectors.databricks import (
    _compute_class_sql,
    _csv_source_sql,
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


def _csv_row(**overrides: str) -> str:
    cols = list(sql_mapping.FOCUS_COLUMNS) + ["x_RecordId", "x_RecordType"]
    return ",".join(overrides.get(c, "") for c in cols)


def test_vectorized_mapping_keeps_corrections_distinct_and_stamps_compute_class(  # type: ignore[no-untyped-def]
    tmp_path,
) -> None:
    """The vectorized ingest() path (DuckDB reading CSV chunks directly, see
    ``DatabricksConnector.ingest``) must reproduce what the old row-based ``fetch()``
    did: x_compute_class from the SKU, x_effective_is_list from the connector, and —
    critically — x_record_id/x_record_type feeding the dedupe key so two DIFFERENT
    correction records for the same charge don't collide into one row.
    """
    base = {
        "ProviderName": "Databricks",
        "BillingAccountId": "acct-1",
        "SubAccountId": "ws-1",
        "BillingPeriodStart": "2026-06-01",
        "BillingPeriodEnd": "2026-06-30",
        "ChargePeriodStart": "2026-06-15 00:00:00",
        "ChargePeriodEnd": "2026-06-15 01:00:00",
        "BillingCurrency": "USD",
        "ChargeCategory": "Usage",
        "ChargeClass": "Correction",
        "ServiceCategory": "Analytics",
        "ServiceName": "ALL_PURPOSE",
        "SkuId": "STANDARD_ALL_PURPOSE_COMPUTE",
        "ResourceId": "cluster-1",
        "ConsumedQuantity": "1",
        "ConsumedUnit": "DBU",
        "Tags": "{}",
    }
    # Same charge line, two distinct correction records — must both survive.
    retraction = _csv_row(**base, BilledCost="-10", EffectiveCost="-10", ListCost="-10",
                           ContractedCost="-10", x_RecordId="rec-A", x_RecordType="RETRACTION")
    restatement = _csv_row(**base, BilledCost="-20", EffectiveCost="-20", ListCost="-20",
                            ContractedCost="-20", x_RecordId="rec-B", x_RecordType="RESTATEMENT")
    # A distinct charge, serverless SKU — proves the compute-class classifier.
    serverless = _csv_row(
        **{
            **base,
            "ChargePeriodStart": "2026-06-16 00:00:00",
            "ChargePeriodEnd": "2026-06-16 01:00:00",
            "ChargeClass": "",
            "SkuId": "ENTERPRISE_JOBS_SERVERLESS_COMPUTE",
            "ServiceName": "JOBS",
        },
        BilledCost="5", EffectiveCost="5", ListCost="5", ContractedCost="5",
        x_RecordId="rec-C", x_RecordType="ORIGINAL",
    )
    csv_path = tmp_path / "chunk.csv"
    csv_path.write_text("\n".join([retraction, restatement, serverless]) + "\n")

    columns = list(sql_mapping.FOCUS_COLUMNS) + ["x_RecordId", "x_RecordType"]
    con = duckdb.connect()
    sql_mapping.ensure_helpers(con)
    source_sql = _csv_source_sql([str(csv_path)], columns)
    present = sql_mapping.present_columns(con, source_sql)
    mapped = sql_mapping.mapping_sql(
        source_sql,
        connector="databricks",
        run_id="r1",
        focus_version="1.3",
        present=present,
        compute_class_sql=_compute_class_sql(),
        effective_is_list=True,
    )
    rows = con.execute(
        f"SELECT x_record_id, x_record_type, x_compute_class, x_effective_is_list, "  # noqa: S608
        f"dedupe_key FROM ({mapped}) ORDER BY x_record_id"
    ).fetchall()

    assert len(rows) == 3  # retraction + restatement didn't collide
    by_id = {r[0]: r for r in rows}
    assert by_id["rec-A"][1] == "RETRACTION"
    assert by_id["rec-B"][1] == "RESTATEMENT"
    assert by_id["rec-A"][4] != by_id["rec-B"][4]  # distinct dedupe keys
    assert by_id["rec-A"][2] == ComputeClass.CLASSIC.value
    assert by_id["rec-C"][2] == ComputeClass.SERVERLESS.value
    assert all(r[3] for r in rows)  # x_effective_is_list stamped True everywhere
