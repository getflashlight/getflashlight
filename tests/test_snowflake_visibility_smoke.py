"""Smoke every Snowflake Visibility/LeaderBoard data path used by the dashboard.

Pins the live-ACCOUNT_USAGE failure mode: optional tables/columns missing must
return empty frames (or derived results), never Catalog/Binder/TypeError.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pandas as pd
import pytest

from flashlight.core.settings import get_settings
from flashlight.ingest.base import IngestWindow
from flashlight.lake import account_usage
from flashlight.lake.account_usage_schema import AccountUsageBatch

_WINDOW = IngestWindow(date(2026, 7, 1), date(2026, 7, 31))

# Public APIs exercised by Visibility tabs, LeaderBoard, and Home.
_SCREEN_CALLS: list[tuple[str, tuple[object, ...], dict[str, object]]] = [
    ("has_local_data", (), {}),
    ("kpi_summary", (), {}),
    ("cost_breakdown", (), {}),
    ("ai_cost_breakdown", (), {}),
    ("serverless_cost_breakdown", (), {}),
    ("top_tables_storage", (25,), {}),
    ("top_users_hidden_waste", (5,), {}),
    ("tco_monthly_trend_and_forecast", (), {}),
    ("tco_by_month", (), {}),
    ("top_users_daily_credits", (10, "all"), {}),
    ("top_users_daily_credits", (10, "service"), {}),
    ("top_users_daily_credits", (10, "adhoc"), {}),
    ("warehouse_daily_credits", (10,), {}),
    ("warehouse_spend_filtered", ("2026-07-01", "2026-07-31", "day"), {}),
    ("warehouse_spend_filtered", ("2026-07-01", "2026-07-31", "month"), {}),
    ("warehouse_summary_filtered", ("2026-07-01", "2026-07-31"), {}),
    ("idle_warehouses_filtered", ("2026-07-01", "2026-07-31"), {}),
    ("queue_pressure_filtered", ("2026-07-01", "2026-07-31"), {}),
    ("hidden_waste_compute", (), {}),
    ("hidden_waste_storage", (), {}),
    ("hidden_waste_ai", (), {}),
    ("hidden_waste_summary", (), {}),
    ("idle_warehouses", (), {}),
    ("warehouse_cost_efficiency", (), {}),
    ("queue_pressure", (), {}),
    ("query_attributed_cost", (), {}),
    ("expensive_query_patterns", (), {}),
    ("cache_reuse_opportunity", (), {}),
    ("storage_trend", (), {}),
    ("top_tables", (), {}),
    ("unattributed_spend", (), {}),
    ("spend_by_warehouse", (), {}),
    ("spend_by_day", ("2026-07-01", "2026-07-31"), {}),
    ("ai_spend_summary", (), {}),
    ("ai_service_metering", (), {}),
    ("ai_function_usage", (), {}),
    ("ai_search_daily", (), {}),
    ("ai_spend_by_day", (), {}),
    ("serverless_optimization_spend", (), {}),
    ("snowpipe_cost", (), {}),
    ("serverless_task_costs", (), {}),
    ("data_transfer_drivers", (), {}),
    ("executive_cost_trend", (), {}),
    ("all_service_cost_profile", (), {}),
    ("cost_breakdown_monthly", (12,), {}),
    ("leaderboard_snapshot", (), {}),
]


@pytest.fixture
def lake_home(tmp_path, monkeypatch) -> Iterator[object]:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _seed_minimal_live_shaped_lake() -> None:
    """Minimal live-shaped dump: required tables only, no optional demos/tags."""
    metrics_json = (
        '[{"key": {"metric": "input", "unit": "tokens"}, "value": 10},'
        ' {"key": {"metric": "output", "unit": "tokens"}, "value": 2}]'
    )
    account_usage.write_account_usage(
        _WINDOW,
        [
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="warehouse_metering_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(
                            ["2026-07-15T10:00:00", "2026-07-16T10:00:00"]
                        ),
                        "warehouse_name": ["ETL_PROD", "ETL_PROD"],
                        "warehouse_id": [1, 1],
                        "credits_used": [20.0, 10.0],
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
                        "name": ["PIPE_A"],
                        "entity_type": ["PIPE"],
                        "credits_used": [1.5],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="metering_daily_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "usage_date": [date(2026, 7, 15)],
                        "service_type": ["WAREHOUSE_METERING"],
                        "credits_used": [30.0],
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
                        "avg_running": [0.05],
                        "avg_queued_load": [0.0],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="query_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "query_id": ["q1", "q2", "q3"],
                        "query_hash": ["h1", "h1", "h1"],
                        "query_parameterized_hash": ["p1", "p1", "p1"],
                        "user_name": ["alice", "bob", "alice"],
                        "warehouse_name": ["ETL_PROD", "ETL_PROD", "ETL_PROD"],
                        "start_time": pd.to_datetime(
                            [
                                "2026-07-15T10:00:00",
                                "2026-07-16T10:00:00",
                                "2026-07-17T10:00:00",
                            ]
                        ),
                        "execution_status": ["SUCCESS", "SUCCESS", "SUCCESS"],
                        "query_type": ["SELECT", "SELECT", "SELECT"],
                        "bytes_scanned": [2 * 1024**4, 1024**4, 1024**4],
                        "bytes_spilled_to_local_storage": [0, 0, 0],
                        "bytes_spilled_to_remote_storage": [0, 10, 0],
                        "percentage_scanned_from_cache": [5.0, 10.0, 8.0],
                        "queued_overload_time": [1000, 0, 500],
                        "total_elapsed_time": [400_000, 10_000, 20_000],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="query_attribution_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "query_id": ["q1", "q2", "q3"],
                        "query_parameterized_hash": ["p1", "p1", "p1"],
                        "credits_attributed_compute": [5.0, 3.0, 2.0],
                        "credits_used_query_acceleration": [0.0, 0.0, 0.0],
                        "warehouse_name": ["ETL_PROD", "ETL_PROD", "ETL_PROD"],
                        "start_time": pd.to_datetime(
                            [
                                "2026-07-15T10:00:00",
                                "2026-07-16T10:00:00",
                                "2026-07-17T10:00:00",
                            ]
                        ),
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
                        "failsafe_bytes": [0],
                        "hybrid_table_storage_bytes": [0],
                        "archive_storage_cool_bytes": [0],
                        "archive_storage_cold_bytes": [0],
                    }
                ),
            ),
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
                        "retained_for_clone_bytes": [0, 0],
                        "is_transient": ["NO", "NO"],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="cortex_ai_functions_usage_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "start_time": pd.to_datetime(["2026-07-15T10:00:00"]),
                        "function_name": ["AI_COMPLETE"],
                        "model_name": ["mistral-7b"],
                        "user_id": [1],
                        "credits": [0.05],
                        "metrics": [metrics_json],
                        "is_completed": [True],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="pipe_usage_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "pipe_name": ["PIPE_A"],
                        "credits_used": ["1.25"],
                        "bytes_inserted": [1024**3],
                        "files_inserted": ["3"],
                    }
                ),
            ),
        ],
    )


def _assert_screen_call(
    visibility_data: object,
    name: str,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> None:
    result = getattr(visibility_data, name)(*args, **kwargs)
    if isinstance(result, pd.DataFrame):
        # UI label helpers previously blew up on NaN table/object names.
        for col in ("table_name", "object_name"):
            if col in result.columns and not result.empty:
                assert result[col].map(
                    lambda v: v is None or isinstance(v, str) or (isinstance(v, float) and v != v)
                ).all()
                assert not result[col].map(lambda v: isinstance(v, float) and v == v).any()
        for col in ("cost_usd", "credits", "wasted_cost_usd", "monthly_cost_usd", "tco"):
            if col in result.columns and not result.empty:
                pd.to_numeric(result[col], errors="raise")
    elif isinstance(result, dict):
        if name == "leaderboard_snapshot":
            for key in (
                "kpis",
                "ai",
                "sw",
                "forecast",
                "monthly",
                "breakdown",
                "top_tables_storage",
                "hidden_waste_compute",
                "top_users_hidden_waste",
            ):
                assert key in result
            kpis = result["kpis"]
            _ = f"${kpis['total_cost']:,.0f}"
            _ = f"{kpis['storage_tb']:.0f}"
            _ = f"{result['sw']['waste_pct']:.0f}"
        elif name == "tco_by_month":
            for month, value in result.items():
                assert hasattr(month, "year")
                float(value)
        else:
            for key in ("total_cost", "ai_cost", "total", "waste_pct"):
                if key in result and result[key] is not None:
                    float(result[key])
    elif isinstance(result, list):
        for item in result:
            float(item["cost"])


def test_all_visibility_screens_against_live_shaped_lake(lake_home: object) -> None:
    """Every screen API must succeed with a live-shaped dump (no demo waste/tags)."""
    from flashlight.dashboard.snowflake import visibility_data

    _seed_minimal_live_shaped_lake()
    visibility_data._as_of.cache_clear()
    assert visibility_data.has_local_data()
    assert not visibility_data._has_table("tag_references")
    assert not visibility_data._has_table("hidden_waste_compute")
    assert not visibility_data._has_column("cortex_ai_functions_usage_history", "calls")

    errors: list[str] = []
    for name, args, kwargs in _SCREEN_CALLS:
        try:
            _assert_screen_call(visibility_data, name, args, kwargs)
        except Exception as exc:  # noqa: BLE001 — collect all failures
            errors.append(f"{name}{args}: {type(exc).__name__}: {exc}")
    assert not errors, "Visibility screen failures:\n" + "\n".join(errors)


def test_visibility_screens_without_optional_attribution(lake_home: object) -> None:
    """Heatmap/query attribution panels must degrade when attribution is absent."""
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
                        "warehouse_id": [1],
                        "credits_used": [12.0],
                    }
                ),
            ),
            AccountUsageBatch(
                provider_name="Snowflake",
                table_name="query_history",
                charge_month="2026-07",
                frame=pd.DataFrame(
                    {
                        "query_id": ["q1"],
                        "user_name": ["alice"],
                        "warehouse_name": ["ETL_PROD"],
                        "start_time": pd.to_datetime(["2026-07-15T10:00:00"]),
                        "execution_status": ["SUCCESS"],
                    }
                ),
            ),
        ],
    )
    visibility_data._as_of.cache_clear()
    assert visibility_data.top_users_daily_credits().empty
    assert visibility_data.query_attributed_cost().empty
    assert visibility_data.warehouse_cost_efficiency().empty
    assert isinstance(visibility_data.unattributed_spend(), pd.DataFrame)
