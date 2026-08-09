"""Snowflake connector mapping, config, and registry wiring."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from flashlight.focus.enums import ChargeCategory, ProviderName, ServiceCategory
from flashlight.ingest.config import SnowflakeConfig, load_connections, scoped_env_name
from flashlight.ingest.connectors._snowflake_supported_drivers import check_support_status
from flashlight.ingest.connectors.snowflake import SnowflakeConnector
from flashlight.ingest.runner import build_connector


def test_snowflake_config_scopes_default_secret_env_names() -> None:
    cfg = SnowflakeConfig(account="xy12345", name="Prod org")
    assert cfg.user_env == scoped_env_name("SNOWFLAKE_USER", name="Prod org", ctype="snowflake")
    assert cfg.password_env == scoped_env_name(
        "SNOWFLAKE_PASSWORD", name="Prod org", ctype="snowflake"
    )


def test_snowflake_config_keeps_explicit_env_names() -> None:
    cfg = SnowflakeConfig(
        account="xy12345",
        name="Prod org",
        user_env="MY_SF_USER",
        password_env="MY_SF_PASSWORD",
    )
    assert cfg.user_env == "MY_SF_USER"
    assert cfg.password_env == "MY_SF_PASSWORD"


def test_load_connections_registers_snowflake(tmp_path: Path) -> None:
    path = tmp_path / "connections.yml"
    path.write_text(
        "connectors:\n"
        "  - type: snowflake\n"
        "    enabled: true\n"
        "    name: Org\n"
        "    account: xy12345.us-east-1\n"
        "    role: ACCOUNTADMIN\n"
    )
    configs = load_connections(str(path))
    assert len(configs) == 1
    assert isinstance(configs[0], SnowflakeConfig)
    assert configs[0].account == "xy12345.us-east-1"


def test_live_dashboard_detects_enabled_snowflake_connection(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The dashboard must select live data instead of its optional demo files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "connections.yml").write_text(
        "connectors:\n  - type: snowflake\n    enabled: true\n    account: xy12345.us-east-1\n"
    )
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))

    from flashlight.dashboard.snowflake import live_data

    assert live_data.is_configured() is True


def test_build_connector_returns_real_snowflake_connector() -> None:
    cfg = SnowflakeConfig(account="xy12345", name="Org")
    connector = build_connector(cfg)
    assert isinstance(connector, SnowflakeConnector)
    assert connector.name == "Org"


def test_map_row_builds_focus_record() -> None:
    connector = SnowflakeConnector(SnowflakeConfig(account="xy12345"))
    record = connector._map_row(
        {
            "USAGE_DATE": date(2026, 3, 15),
            "USAGE_IN_CURRENCY": "12.50",
            "IS_ADJUSTMENT": False,
            "SERVICE_TYPE": "WAREHOUSE_METERING",
            "USAGE_TYPE": "compute",
            "ORGANIZATION_NAME": "ACME",
            "ACCOUNT_NAME": "PROD",
            "ACCOUNT_LOCATOR": "XY12345",
            "CURRENCY": "USD",
            "REGION": "us-east-1",
            "USAGE": 4.0,
        }
    )
    assert record is not None
    assert record.provider_name == ProviderName.SNOWFLAKE
    assert record.billing_account_id == "ACME"
    assert record.sub_account_id == "PROD"
    assert record.billing_period_start == date(2026, 3, 1)
    assert record.billing_period_end == date(2026, 4, 1)
    assert record.charge_period_start == datetime(2026, 3, 15)
    assert record.charge_period_end == datetime(2026, 3, 16)
    assert record.billing_currency == "USD"
    assert record.effective_cost == Decimal("12.50")
    assert record.charge_category == ChargeCategory.USAGE
    assert record.service_category == ServiceCategory.COMPUTE
    assert record.service_name == "WAREHOUSE_METERING"
    assert record.x_source_connector == "snowflake"


def test_map_row_marks_adjustment() -> None:
    connector = SnowflakeConnector(SnowflakeConfig(account="xy12345"))
    record = connector._map_row(
        {
            "USAGE_DATE": "2026-03-01",
            "USAGE_IN_CURRENCY": 1,
            "IS_ADJUSTMENT": True,
            "SERVICE_TYPE": "STORAGE",
            "USAGE_TYPE": "storage",
            "ORGANIZATION_NAME": "ACME",
            "CURRENCY": "USD",
            "USAGE": 0,
        }
    )
    assert record is not None
    assert record.charge_category == ChargeCategory.ADJUSTMENT
    assert record.service_category == ServiceCategory.STORAGE


def test_map_row_skips_null_usage_date() -> None:
    connector = SnowflakeConnector(SnowflakeConfig(account="xy12345"))
    assert connector._map_row({"USAGE_DATE": None}) is None


def test_map_driver_health_sets_support_status() -> None:
    connector = SnowflakeConnector(SnowflakeConfig(account="xy12345"))
    rec = connector._map_driver_health_row(
        {
            "CHARGE_MONTH": date(2026, 3, 1),
            "CLIENT_DRIVER": "PythonConnector 3.6.0",
            "CLIENT_APPLICATION": "dbt",
            "EXECUTED_BY": "alice",
            "QUERY_COUNT": 9,
        }
    )
    assert rec.provider_name == "Snowflake"
    assert rec.charge_month == date(2026, 3, 1)
    assert rec.query_count == 9
    assert rec.support_status == "unsupported"  # below 3.7.0 minimum


def test_check_support_status_known_versions() -> None:
    assert check_support_status("PythonConnector 4.7.1") == "supported"
    assert check_support_status("PythonConnector 3.6.0") == "unsupported"
    assert check_support_status("Snowsight") == "unknown"
    assert check_support_status(None) == "unknown"
