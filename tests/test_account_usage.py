"""ACCOUNT_USAGE lake — Visibility serves local Parquet after ingest, never live Snowflake."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pandas as pd
import pytest

from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.lake import account_usage, paths
from flashlight.lake.account_usage_schema import AccountUsageBatch


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


_WINDOW = IngestWindow(date(2026, 7, 1), date(2026, 7, 31))


def _metering_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "START_TIME": pd.to_datetime(["2026-07-15T10:00:00", "2026-07-16T10:00:00"]),
            "WAREHOUSE_NAME": ["ETL_PROD", "ETL_PROD"],
            "WAREHOUSE_SIZE": ["Large", "Large"],
            "CREDITS_USED": [12.5, 8.0],
        }
    )


def test_write_account_usage_partition_replace(lake_home) -> None:  # type: ignore[no-untyped-def]
    batch = AccountUsageBatch(
        provider_name="Snowflake",
        table_name="warehouse_metering_history",
        charge_month="2026-07",
        frame=_metering_frame(),
    )
    assert account_usage.write_account_usage(_WINDOW, [batch]) == 2
    paths_found = account_usage.table_parquet_paths("warehouse_metering_history")
    assert len(paths_found) == 1
    assert paths_found[0].exists()

    # Re-write replaces the partition.
    smaller = _metering_frame().iloc[:1]
    batch2 = AccountUsageBatch(
        provider_name="Snowflake",
        table_name="warehouse_metering_history",
        charge_month="2026-07",
        frame=smaller,
    )
    assert account_usage.write_account_usage(_WINDOW, [batch2]) == 1
    df = pd.read_parquet(paths_found[0])
    assert len(df) == 1
    assert list(df.columns) == [c.lower() for c in df.columns]


def test_visibility_reads_lake_without_snowflake_connect(
    lake_home: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dashboard Visibility must not open snowflake.connector after lake install."""
    from flashlight.dashboard.snowflake import visibility_data

    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_metering_history",
                charge_month="2026-07",
                frame=_metering_frame(),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="metering_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15"]),
                        "service_type": ["SNOWPIPE"],
                        "credits_used": [1.0],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_load_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15"]),
                        "warehouse_name": ["ETL_PROD"],
                        "avg_running": [0.2],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="query_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15"]),
                        "execution_status": ["SUCCESS"],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="storage_usage",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "usage_date": [date(2026, 7, 15)],
                        "storage_bytes": [1_000_000_000_000],
                        "stage_bytes": [0],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="table_storage_metrics",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "table_catalog": ["DB"],
                        "table_schema": ["PUBLIC"],
                        "table_name": ["T"],
                        "active_bytes": [1],
                        "time_travel_bytes": [0],
                        "failsafe_bytes": [0],
                    }
                ),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()

    def _boom(*_a: object, **_k: object) -> None:
        raise AssertionError("dashboard must not call snowflake.connector.connect")

    monkeypatch.setattr("snowflake.connector.connect", _boom)
    assert visibility_data.has_local_data()
    kpis = visibility_data.kpi_summary()
    assert kpis["total_credits"] > 0
    # Derived waste must work from raw tables without opening Snowflake.
    compute = visibility_data.hidden_waste_compute()
    assert isinstance(compute, pd.DataFrame)


def test_top_users_hidden_waste_without_demo_waste_tables(
    lake_home: object,
) -> None:
    """Attribution path must work when only raw ACCOUNT_USAGE tables are present."""
    from flashlight.dashboard.snowflake import visibility_data

    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_metering_history",
                charge_month="2026-07",
                frame=_metering_frame(),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_load_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15"]),
                        "warehouse_name": ["ETL_PROD"],
                        "avg_running": [0.05],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="query_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "query_id": ["q1", "q2"],
                        "user_name": ["alice", "bob"],
                        "warehouse_name": ["ETL_PROD", "ETL_PROD"],
                        "start_time": pd.to_datetime(
                            ["2026-07-15T10:00:00", "2026-07-16T10:00:00"]
                        ),
                        "execution_status": ["SUCCESS", "SUCCESS"],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="query_attribution_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "query_id": ["q1", "q2"],
                        "credits_attributed_compute": [8.0, 4.0],
                    }
                ),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    assert not visibility_data._has_table("hidden_waste_compute")
    top = visibility_data.top_users_hidden_waste()
    assert isinstance(top, pd.DataFrame)


def test_fetch_account_usage_failure_does_not_raise_in_runner(
    lake_home: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from flashlight.ingest import runner
    from flashlight.ingest.base import Connector

    class Boom(Connector):
        name = "boom"

        def fetch_account_usage(
            self, window: IngestWindow
        ) -> Iterator[AccountUsageBatch]:
            raise RuntimeError("no account usage access")
            yield  # pragma: no cover

    written = runner._run_account_usage(_WINDOW, [Boom()])
    assert written == 0


def test_install_flat_parquets_into_lake(lake_home: object, tmp_path: object) -> None:
    src = tmp_path / "synth"  # type: ignore[operator]
    src.mkdir()
    pd.DataFrame({"credits_used": [1.0]}).to_parquet(src / "warehouse_metering_history.parquet")
    n = account_usage.install_flat_parquets(src, charge_month="2026-08")
    assert n == 1
    assert account_usage.has_parquet()
    assert paths.account_usage_dir().joinpath(
        "provider_name=Snowflake",
        "warehouse_metering_history",
        "charge_month=2026-08",
        "data.parquet",
    ).exists()


def test_kpi_summary_accepts_decimal_credits(lake_home: object) -> None:
    """Live Snowflake numerics arrive as Decimal — must not TypeError with CREDIT_PRICE."""
    from decimal import Decimal

    from flashlight.dashboard.snowflake import visibility_data

    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_metering_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15T10:00:00"]),
                        "warehouse_name": ["ETL_PROD"],
                        "credits_used": [Decimal("12.5")],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="metering_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15T10:00:00"]),
                        "service_type": ["SNOWPIPE"],
                        "credits_used": [Decimal("1.5")],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_load_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15T10:00:00"]),
                        "warehouse_name": ["ETL_PROD"],
                        "avg_running": [Decimal("0.2")],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="query_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15T10:00:00"]),
                        "execution_status": ["SUCCESS"],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="storage_usage",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "usage_date": [date(2026, 7, 15)],
                        "storage_bytes": [Decimal("1000000000000")],
                        "stage_bytes": [Decimal("0")],
                        "failsafe_bytes": [Decimal("0")],
                    }
                ),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    kpis = visibility_data.kpi_summary()
    assert isinstance(kpis["total_cost"], (int, float))
    assert kpis["total_credits"] > 0


def test_cost_breakdown_monthly_mixed_tz_months(lake_home: object) -> None:
    """Live dumps mix tz-aware start_time with naive usage_date — must not TypeError."""
    from flashlight.dashboard.snowflake import visibility_data

    aware = pd.to_datetime(["2026-07-15T10:00:00+00:00", "2026-06-15T10:00:00+00:00"])
    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_metering_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": aware,
                        "warehouse_name": ["ETL_PROD", "ETL_PROD"],
                        "credits_used": [10.0, 5.0],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="metering_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": aware,
                        "service_type": ["SNOWPIPE", "SNOWPIPE"],
                        "credits_used": [1.0, 1.0],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="storage_usage",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "usage_date": [date(2026, 7, 15), date(2026, 6, 15)],
                        "storage_bytes": [1_000_000_000_000, 1_000_000_000_000],
                        "stage_bytes": [0, 0],
                        "failsafe_bytes": [0, 0],
                    }
                ),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    df = visibility_data.cost_breakdown_monthly(12)
    assert not df.empty
    assert set(df["month"].astype(str)) <= {"2026-06", "2026-07"}


def test_hidden_waste_compute_without_warehouse_size_column(
    lake_home: object,
) -> None:
    """Live WAREHOUSE_METERING_HISTORY has no warehouse_size — must not BinderError."""
    from flashlight.dashboard.snowflake import visibility_data

    metering = pd.DataFrame(
        {
            "start_time": pd.to_datetime(["2026-07-15T10:00:00"] * 2),
            "warehouse_name": ["ETL_PROD", "ETL_PROD"],
            "warehouse_id": [1, 1],
            "credits_used": [20.0, 15.0],
            "credits_used_compute": [18.0, 13.0],
            "credits_used_cloud_services": [2.0, 2.0],
        }
    )
    load = pd.DataFrame(
        {
            "start_time": pd.to_datetime(["2026-07-15T10:00:00"]),
            "warehouse_name": ["ETL_PROD"],
            "avg_running": [0.05],
        }
    )
    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_metering_history",
                charge_month="2026-07",
                frame=metering,
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_load_history",
                charge_month="2026-07",
                frame=load,
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    df = visibility_data.hidden_waste_compute()
    assert not df.empty
    assert "size" in df.columns
    assert df.iloc[0]["size"] == "UNKNOWN"


def test_ai_function_usage_live_metrics_schema(lake_home: object) -> None:
    """Live CORTEX_AI_FUNCTIONS_USAGE_HISTORY uses metrics JSON, not calls/tokens_*."""
    from flashlight.dashboard.snowflake import visibility_data

    metrics = (
        '[{"key": {"metric": "input", "unit": "tokens"}, "value": 100},'
        ' {"key": {"metric": "output", "unit": "tokens"}, "value": 20}]'
    )
    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="cortex_ai_functions_usage_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15T10:00:00"] * 2),
                        "function_name": ["AI_COMPLETE", "AI_COMPLETE"],
                        "model_name": ["mistral-7b", "mistral-7b"],
                        "user_id": [1, 2],
                        "credits": [0.01, 0.02],
                        "metrics": [metrics, metrics],
                        "is_completed": [True, True],
                    }
                ),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    assert not visibility_data._has_column("cortex_ai_functions_usage_history", "calls")
    df = visibility_data.ai_function_usage()
    assert len(df) == 1
    assert df.iloc[0]["object_name"] == "AI_COMPLETE:mistral-7b"
    assert int(df.iloc[0]["calls"]) == 2
    assert int(df.iloc[0]["tokens_sent"]) == 200
    assert int(df.iloc[0]["tokens_received"]) == 40
    assert int(df.iloc[0]["users"]) == 2


def test_top_tables_storage_null_catalog(lake_home: object) -> None:
    """Live TABLE_STORAGE_METRICS can have NULL catalog — must not yield NaN names."""
    from flashlight.dashboard.snowflake import visibility_data

    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="table_storage_metrics",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "table_catalog": [None, "DB"],
                        "table_schema": ["PUBLIC", "PUBLIC"],
                        "table_name": ["ORPHAN", "T"],
                        "active_bytes": [5_000_000_000, 1_000_000_000],
                        "time_travel_bytes": [0, 0],
                        "failsafe_bytes": [0, 0],
                    }
                ),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    df = visibility_data.top_tables_storage(25)
    assert not df.empty
    assert df["table_name"].map(lambda x: isinstance(x, str)).all()
    assert None not in list(df["table_name"])
    assert not df["table_name"].isna().any()


def test_optional_account_usage_tables_return_empty(lake_home: object) -> None:
    """Missing optional ACCOUNT_USAGE dumps must not CatalogError the dashboard."""
    from flashlight.dashboard.snowflake import visibility_data

    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_metering_history",
                charge_month="2026-07",
                frame=_metering_frame(),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    assert visibility_data.ai_search_daily().empty
    assert visibility_data.serverless_optimization_spend().empty
    assert visibility_data.serverless_task_costs().empty
    assert visibility_data.data_transfer_drivers().empty
