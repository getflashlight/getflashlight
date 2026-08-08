from datetime import date

from flashlight.ingest.connectors.databricks import DatabricksConnector
from flashlight.lake.compute_instance_schema import (
    ComputeInstanceRecord,
    build_table,
    empty_table,
)


def test_compute_instance_schema_round_trip() -> None:
    records = [
        ComputeInstanceRecord(
            provider_name="Databricks",
            charge_month=date(2026, 7, 15),  # normalized to the 1st
            cluster_id="0000-123456-crmpt124",
            cluster_name="etl-prod",
            owner_user="alice@example.com",
            instance_id="i-1234a6c12a2681234",
            is_driver=True,
            node_type="i3.xlarge",
            x_source_connector="databricks",
        ),
    ]
    table = build_table(records)
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["cluster_id"] == "0000-123456-crmpt124"
    assert row["cluster_name"] == "etl-prod"
    assert row["owner_user"] == "alice@example.com"
    assert row["instance_id"] == "i-1234a6c12a2681234"
    assert row["is_driver"] is True
    assert row["node_type"] == "i3.xlarge"
    assert row["provider_name"] == "Databricks"
    assert row["charge_month"] == "2026-07"  # first-of-month normalization


def test_compute_instance_empty_table_is_typed() -> None:
    table = empty_table()
    assert table.num_rows == 0
    assert table.schema.names == build_table([]).schema.names


def test_to_compute_instance_maps_row() -> None:
    row = {
        "charge_month": "2026-07-01",
        "cluster_id": "0000-123456-crmpt124",
        "cluster_name": "etl-prod",
        "owner_user": "alice@example.com",
        "instance_id": "i-1234a6c12a2681234",
        "is_driver": "false",
        "node_type": "i3.xlarge",
    }
    rec = DatabricksConnector._to_compute_instance(row)
    assert rec is not None
    assert rec.provider_name == "Databricks"
    assert rec.charge_month == date(2026, 7, 1)
    assert rec.cluster_id == "0000-123456-crmpt124"
    assert rec.cluster_name == "etl-prod"
    assert rec.owner_user == "alice@example.com"
    assert rec.instance_id == "i-1234a6c12a2681234"
    assert rec.is_driver is False
    assert rec.node_type == "i3.xlarge"


def test_to_compute_instance_maps_row_without_cluster_metadata() -> None:
    """cluster_name/owner_user are absent when system.compute.clusters had no row for
    this cluster_id (LEFT JOIN, so the cost row survives with NULLs, not a dropped row)."""
    row = {
        "charge_month": "2026-07-01",
        "cluster_id": "0000-123456-crmpt124",
        "instance_id": "i-1234a6c12a2681234",
        "is_driver": "true",
        "node_type": "i3.xlarge",
    }
    rec = DatabricksConnector._to_compute_instance(row)
    assert rec is not None
    assert rec.cluster_name is None
    assert rec.owner_user is None


def test_to_compute_instance_skips_row_without_charge_month() -> None:
    assert DatabricksConnector._to_compute_instance({"cluster_id": "c1"}) is None


def test_to_compute_instance_skips_row_without_cluster_id() -> None:
    assert (
        DatabricksConnector._to_compute_instance(
            {"charge_month": "2026-07-01", "instance_id": "i-1"}
        )
        is None
    )


def test_to_compute_instance_skips_row_without_instance_id() -> None:
    assert (
        DatabricksConnector._to_compute_instance(
            {"charge_month": "2026-07-01", "cluster_id": "c1"}
        )
        is None
    )
