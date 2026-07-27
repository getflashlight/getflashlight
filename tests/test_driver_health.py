from datetime import date

from flashlight.ingest.connectors.databricks import DatabricksConnector
from flashlight.lake.driver_health_schema import (
    DriverHealthRecord,
    build_table,
    empty_table,
)


def test_driver_health_schema_round_trip() -> None:
    records = [
        DriverHealthRecord(
            provider_name="Databricks",
            charge_month=date(2026, 7, 15),  # normalized to the 1st
            client_driver="DatabricksJDBCDriver, 2.7.1",
            client_application="Retool",
            executed_by="alice@example.com",
            query_count=33,
            x_source_connector="databricks",
        ),
    ]
    table = build_table(records)
    assert table.num_rows == 1
    row = table.to_pylist()[0]
    assert row["client_driver"] == "DatabricksJDBCDriver, 2.7.1"
    assert row["client_application"] == "Retool"
    assert row["executed_by"] == "alice@example.com"
    assert row["query_count"] == 33
    assert row["provider_name"] == "Databricks"
    assert row["charge_month"] == "2026-07"  # first-of-month normalization


def test_driver_health_empty_table_is_typed() -> None:
    table = empty_table()
    assert table.num_rows == 0
    assert table.schema.names == build_table([]).schema.names


def test_to_driver_health_maps_row() -> None:
    row = {
        "charge_month": "2026-07-01",
        "client_driver": "PyDatabricksSqlConnector, 4.2.5",
        "client_application": "MonteCarlo",
        "executed_by": "svc-monte-carlo",
        "query_count": "9848",
    }
    rec = DatabricksConnector._to_driver_health(row)
    assert rec is not None
    assert rec.provider_name == "Databricks"
    assert rec.charge_month == date(2026, 7, 1)
    assert rec.client_driver == "PyDatabricksSqlConnector, 4.2.5"
    assert rec.client_application == "MonteCarlo"
    assert rec.executed_by == "svc-monte-carlo"
    assert rec.query_count == 9848


def test_to_driver_health_skips_row_without_charge_month() -> None:
    assert DatabricksConnector._to_driver_health({"client_driver": "x"}) is None
