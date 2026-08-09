from datetime import date

import pandas as pd

from flashlight.dashboard.views.driver_health import (
    _COLS,
    _OUTDATED_COLS,
    _parse_driver,
    _with_staleness,
)
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
            cluster_id="analytics-prod",
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
    assert row["cluster_id"] == "analytics-prod"
    assert row["query_count"] == 33
    assert row["support_status"] is None
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


def test_parse_driver_handles_redshift_space_separated_versions() -> None:
    assert _parse_driver("Redshift JDBC Driver 2.0.0.0") == ("Redshift JDBC Driver", "2.0.0.0")
    assert _parse_driver("Amazon Redshift ODBC Driver 1.4.15.0001") == (
        "Amazon Redshift ODBC Driver",
        "1.4.15.0001",
    )


def test_driver_health_tables_omit_empty_application_column() -> None:
    assert "client_application" not in _COLS
    assert "client_application" not in _OUTDATED_COLS


def test_staleness_comparison_does_not_cross_clusters() -> None:
    records = pd.DataFrame(
        [
            {
                "provider_name": "AWS",
                "cluster_id": "analytics",
                "client_driver": "Redshift JDBC Driver 2.0.0.0",
            },
            {
                "provider_name": "AWS",
                "cluster_id": "reporting",
                "client_driver": "Redshift JDBC Driver 2.2.7",
            },
        ]
    )

    enriched = _with_staleness(records)

    assert enriched["status"].tolist() == ["up_to_date", "up_to_date"]


def test_driver_health_flows_from_typed_bronze_to_gold(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.core.settings import get_settings
    from flashlight.gold.reader import query_view
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import driver_health, paths
    from flashlight.transform.runner import build_gold

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    window = IngestWindow(date(2026, 7, 1), date(2026, 7, 31))
    driver_health.write_driver_health(
        window,
        [
            DriverHealthRecord(
                provider_name="AWS", charge_month=date(2026, 7, 1),
                client_driver="Redshift JDBC Driver 2.0.0.0", cluster_id="analytics-prod",
                query_count=44,
                x_source_connector="Prod",
            )
        ],
    )
    assert list(paths.bronze_driver_health_dir().glob("**/*.parquet"))
    build_gold()
    assert query_view("driver_health.driver_health")[0]["query_count"] == 44
    get_settings.cache_clear()


def test_redshift_policy_config_flows_from_bronze_to_gold(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from flashlight.core.settings import get_settings
    from flashlight.gold.reader import query_view
    from flashlight.ingest.base import IngestWindow
    from flashlight.lake import redshift_policy_config
    from flashlight.lake.redshift_policy_config_schema import RedshiftPolicyConfigRecord
    from flashlight.transform.runner import build_gold

    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    window = IngestWindow(date(2026, 7, 1), date(2026, 7, 31))
    redshift_policy_config.write(
        window,
        [
            RedshiftPolicyConfigRecord(
                snapshot_month=date(2026, 7, 1), cluster_id="prod", encrypted=True,
                publicly_accessible=False, enhanced_vpc_routing=True,
                automated_snapshot_retention_days=7, require_ssl=True, tag_count=2,
                x_source_connector="Prod",
            )
        ],
    )
    build_gold()
    rows = query_view("policy.policy_record")
    by_category = {row["policy_category"]: row for row in rows}
    assert by_category["redshift_encryption"]["status"] == "compliant"
    assert by_category["redshift_require_ssl"]["status"] == "compliant"
    assert by_category["redshift_snapshot_retention"]["status"] == "compliant"
    get_settings.cache_clear()
