"""Snowflake live data layer — same API as snowflake_visibility_data.py but queries the
customer's live ACCOUNT_USAGE views instead of synthetic Parquet files.

Reads the Snowflake connector config from connections.yml and uses the same auth path
(key-pair, authenticator, or password) as the ingest connector. All functions return
empty DataFrames or zeroed dicts gracefully when no data is available.

One page render opens many ACCOUNT_USAGE queries.  Use :func:`live_session` so they
share a single connector (mirrors ``gold_session`` for DuckDB) — otherwise each call
opens a new TCP/TLS session and stalls NiceGUI's event loop until WebSockets die.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv
from snowflake.connector import SnowflakeConnection

from flashlight.ingest.config import SnowflakeConfig, env, load_connections

# live_data.py → snowflake/ → dashboard/ → flashlight/ → src/ → repo root
_REPO_ROOT = Path(__file__).resolve().parents[4]
load_dotenv(_REPO_ROOT / ".env")

# Connector logs every connect() at INFO — LeaderBoard alone would spam dozens of lines.
logging.getLogger("snowflake.connector").setLevel(logging.WARNING)

CREDIT_PRICE = 3.00
_STORAGE_COST_PER_TB = 23.0  # $/TB/month on-demand

# One connection per page-render session (see :func:`live_session`).
_session_conn: ContextVar[SnowflakeConnection | None] = ContextVar(
    "snowflake_live_session_conn", default=None
)


# ── Connection ────────────────────────────────────────────────────────────────

def _sf_config() -> SnowflakeConfig | None:
    """Return the first enabled Snowflake config from connections.yml, or None.

    Tries FLASHLIGHT_HOME first, then falls back to the project-local
    config/connections.yml (for development when FLASHLIGHT_HOME is not set).
    """
    # 1. Standard path: FLASHLIGHT_HOME/config/connections.yml
    try:
        for cfg in load_connections():
            if isinstance(cfg, SnowflakeConfig):
                # Resolve relative private_key_path against FLASHLIGHT_HOME
                if cfg.private_key_path and not Path(cfg.private_key_path).is_absolute():
                    from flashlight.lake.paths import home as _lake_home  # noqa: PLC0415

                    cfg.private_key_path = str(_lake_home() / cfg.private_key_path)
                return cfg
    except Exception:  # noqa: BLE001
        pass

    # 2. Fallback: project-local config/connections.yml
    project_cfg = _REPO_ROOT / "config" / "connections.yml"
    if project_cfg.exists():
        try:
            for cfg in load_connections(str(project_cfg)):
                if isinstance(cfg, SnowflakeConfig):
                    if cfg.private_key_path and not Path(cfg.private_key_path).is_absolute():
                        cfg.private_key_path = str(
                            project_cfg.parent.parent / cfg.private_key_path
                        )
                    return cfg
        except Exception:  # noqa: BLE001
            pass
    return None


def is_configured() -> bool:
    """Whether an enabled Snowflake connection is available to the dashboard.

    This deliberately only checks configuration.  Authentication and query failures
    are handled by the individual data functions so a temporarily unavailable
    account cannot take down the dashboard page.
    """
    return _sf_config() is not None


def _connect(cfg: SnowflakeConfig) -> SnowflakeConnection:
    user = env(cfg.user_env)
    params: dict[str, object] = {
        "account": cfg.account,
        "user": user,
        "role": cfg.role,
        "database": cfg.database,
        "schema": "ACCOUNT_USAGE",
    }
    if cfg.private_key_path:
        key_bytes = Path(cfg.private_key_path).read_bytes()
        private_key = serialization.load_pem_private_key(
            key_bytes, password=None, backend=default_backend()
        )
        pkb = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        params["private_key"] = pkb
    elif cfg.authenticator:
        params["authenticator"] = cfg.authenticator
    else:
        params["password"] = env(cfg.password_env)
    if cfg.warehouse:
        params["warehouse"] = cfg.warehouse
    return snowflake.connector.connect(**params)


@contextmanager
def live_session() -> Iterator[None]:
    """Scope one Snowflake connection to everything run inside this block.

    Nested calls reuse the outer connection.  Outside a session, helpers still
    open/close a one-shot connection (scripts/tests).
    """
    if _session_conn.get() is not None:
        yield
        return
    cfg = _sf_config()
    if cfg is None:
        yield
        return
    conn = _connect(cfg)
    token = _session_conn.set(conn)
    try:
        yield
    finally:
        _session_conn.reset(token)
        conn.close()


@contextmanager
def _borrow_conn() -> Iterator[SnowflakeConnection | None]:
    """Yield the session connection, or a short-lived one-shot connection."""
    existing = _session_conn.get()
    if existing is not None:
        yield existing
        return
    cfg = _sf_config()
    if cfg is None:
        yield None
        return
    conn = _connect(cfg)
    try:
        yield conn
    finally:
        conn.close()


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Lowercase Snowflake result columns — connector returns UPPERCASE by default."""
    if df.columns.empty:
        return df
    out = df.copy()
    out.columns = [str(c).lower() for c in out.columns]
    return out


def _query(sql: str) -> pd.DataFrame:
    """Run sql against the live Snowflake account; return empty DF on any error."""
    try:
        with _borrow_conn() as conn:
            if conn is None:
                return pd.DataFrame()
            cur = conn.cursor()
            try:
                cur.execute(sql)
                return _normalize_columns(cur.fetch_pandas_all())
            finally:
                cur.close()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()


def _fetchone(sql: str) -> tuple[Any, ...] | None:
    """Run sql and return the first row, or None on any error."""
    try:
        with _borrow_conn() as conn:
            if conn is None:
                return None
            cur = conn.cursor()
            try:
                cur.execute(sql)
                return cur.fetchone()
            finally:
                cur.close()
    except Exception:  # noqa: BLE001
        return None


def leaderboard_snapshot() -> dict[str, Any]:
    """All LeaderBoard queries in one live session — safe to run via ``run.io_bound``."""
    with live_session():
        compute = hidden_waste_compute()
        storage = hidden_waste_storage()
        ai_waste = hidden_waste_ai()
        monthly = cost_breakdown_monthly(12)
        return {
            "kpis": kpi_summary(),
            "ai": ai_spend_summary(),
            "sw": hidden_waste_summary(),
            "forecast": tco_monthly_trend_and_forecast(),
            "monthly": monthly,
            "breakdown": cost_breakdown(),
            "ai_cost_breakdown": ai_cost_breakdown(),
            "serverless_cost_breakdown": serverless_cost_breakdown(),
            "top_tables_storage": top_tables_storage(25),
            "hidden_waste_compute": compute,
            "hidden_waste_storage": storage,
            "hidden_waste_ai": ai_waste,
            "top_users_hidden_waste": top_users_hidden_waste(5),
        }

def kpi_summary() -> dict[str, Any]:
    """Top-line KPIs for the overview tab."""
    try:
        with _borrow_conn() as conn:
            if conn is None:
                return _empty_kpi()
            cur = conn.cursor()
            try:
                month_sql = "DATE_TRUNC('month', CURRENT_DATE())"

                def _scalar(sql: str) -> float:
                    cur.execute(sql)
                    row = cur.fetchone()
                    return float((row[0] if row else 0) or 0)

                wh_credits = _scalar(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
                    f"WHERE start_time >= {month_sql}"
                )
                svc_credits = _scalar(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.METERING_HISTORY "
                    f"WHERE start_time >= {month_sql}"
                )
                total_credits = wh_credits + svc_credits

                ai_wh_credits = _scalar(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
                    f"WHERE warehouse_name IN "
                    f"('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS') "
                    f"AND start_time >= {month_sql}"
                )
                serverless_credits = _scalar(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.METERING_HISTORY "
                    f"WHERE start_time >= {month_sql} "
                    f"AND service_type IN ('AUTOMATIC_CLUSTERING','SNOWPIPE','SERVERLESS_TASK',"
                    f"'REPLICATION','SEARCH_OPTIMIZATION','MATERIALIZED_VIEW','QUERY_ACCELERATION',"
                    f"'SNOWPARK_CONTAINER_SERVICES')"
                )

                cur.execute(
                    "SELECT COUNT(DISTINCT warehouse_name) "
                    "FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY"
                )
                wh_row = cur.fetchone()

                cur.execute(
                    "SELECT COUNT(*) FROM ACCOUNT_USAGE.QUERY_HISTORY "
                    "WHERE execution_status = 'SUCCESS'"
                )
                q_row = cur.fetchone()

                cur.execute(
                    "SELECT AVG(avg_running) FROM ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY"
                )
                u_row = cur.fetchone()

                cur.execute(
                    "SELECT ROUND((storage_bytes + stage_bytes) / POWER(1024, 4), 2) "
                    "FROM ACCOUNT_USAGE.STORAGE_USAGE ORDER BY usage_date DESC LIMIT 1"
                )
                s_row = cur.fetchone()

                ytd_wh = _scalar(
                    "SELECT COALESCE(SUM(credits_used), 0) "
                    "FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
                    "WHERE start_time >= DATE_TRUNC('year', CURRENT_DATE())"
                )
                ytd_svc = _scalar(
                    "SELECT COALESCE(SUM(credits_used), 0) "
                    "FROM ACCOUNT_USAGE.METERING_HISTORY "
                    "WHERE start_time >= DATE_TRUNC('year', CURRENT_DATE())"
                )
            finally:
                cur.close()

        storage_tb = float((s_row[0] if s_row else 0) or 0)
        storage_cost = round(storage_tb * _STORAGE_COST_PER_TB, 0)
        today = date.today()
        return {
            "total_credits": round(total_credits, 0),
            "total_cost": round(total_credits * CREDIT_PRICE + storage_cost, 0),
            "compute_cost": round((wh_credits - ai_wh_credits) * CREDIT_PRICE, 0),
            "serverless_compute_cost": round(serverless_credits * CREDIT_PRICE, 0),
            "month_label": today.strftime("%B %Y"),
            "warehouses": int((wh_row[0] if wh_row else 0) or 0),
            "queries": int((q_row[0] if q_row else 0) or 0),
            "avg_utilization_pct": round(float((u_row[0] if u_row else 0) or 0) * 100, 1),
            "storage_tb": storage_tb,
            "storage_cost": storage_cost,
            "ytd_cost": round((ytd_wh + ytd_svc) * CREDIT_PRICE
                              + storage_cost * today.month, 0),
        }
    except Exception:  # noqa: BLE001
        return _empty_kpi()


def _empty_kpi() -> dict[str, Any]:
    today = date.today()
    return {
        "total_credits": 0, "total_cost": 0, "compute_cost": 0,
        "serverless_compute_cost": 0, "month_label": today.strftime("%B %Y"),
        "warehouses": 0, "queries": 0, "avg_utilization_pct": 0.0,
        "storage_tb": 0.0, "storage_cost": 0, "ytd_cost": 0,
    }


# ── Cost Breakdown ────────────────────────────────────────────────────────────

def cost_breakdown() -> list[dict[str, Any]]:
    """Major cost categories for pie chart — current month spend."""
    try:
        with _borrow_conn() as conn:
            if conn is None:
                return []
            cur = conn.cursor()
            try:
                m = "DATE_TRUNC('month', CURRENT_DATE())"

                def _s(sql: str) -> float:
                    cur.execute(sql)
                    row = cur.fetchone()
                    return float((row[0] if row else 0) or 0)

                managed = _s(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
                    f"WHERE start_time >= {m} "
                    f"AND warehouse_name NOT IN "
                    f"('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS')"
                ) * CREDIT_PRICE
                serverless = _s(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.METERING_HISTORY "
                    f"WHERE start_time >= {m} "
                    f"AND service_type IN ('AUTOMATIC_CLUSTERING','SNOWPIPE','SERVERLESS_TASK',"
                    f"'REPLICATION','SEARCH_OPTIMIZATION','MATERIALIZED_VIEW','QUERY_ACCELERATION',"
                    f"'SNOWPARK_CONTAINER_SERVICES')"
                ) * CREDIT_PRICE
                ai_wh = _s(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
                    f"WHERE start_time >= {m} "
                    f"AND warehouse_name IN "
                    f"('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS')"
                )
                ai_svc = _s(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.METERING_HISTORY "
                    f"WHERE start_time >= {m} "
                    f"AND service_type IN ('CORTEX_AI_FUNCTIONS','CORTEX_SEARCH',"
                    f"'AI_SERVICES','CORTEX_ANALYST','DOCUMENT_AI',"
                    f"'SNOWFLAKE_INTELLIGENCE','CORTEX_AGENTS','CORTEX_GUARDRAILS')"
                )
                ai_total = (ai_wh + ai_svc) * CREDIT_PRICE
                cur.execute(
                    "SELECT storage_bytes, stage_bytes, failsafe_bytes "
                    "FROM ACCOUNT_USAGE.STORAGE_USAGE ORDER BY usage_date DESC LIMIT 1"
                )
                st = cur.fetchone()
                tb = 1 / (1024 ** 4)
                storage = (
                    float((st[0] if st else 0) or 0)
                    + float((st[1] if st else 0) or 0)
                    + float((st[2] if st else 0) or 0)
                ) * tb * _STORAGE_COST_PER_TB
                xfer = _s(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.METERING_HISTORY "
                    f"WHERE start_time >= {m} AND service_type = 'DATA_TRANSFER'"
                ) * CREDIT_PRICE
                tco_wh = _s(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY WHERE start_time >= {m}"
                )
                tco_svc = _s(
                    f"SELECT COALESCE(SUM(credits_used), 0) "
                    f"FROM ACCOUNT_USAGE.METERING_HISTORY WHERE start_time >= {m}"
                )
                tco = (tco_wh + tco_svc) * CREDIT_PRICE + storage
                other = max(tco - managed - serverless - ai_total - storage - xfer, 0)
            finally:
                cur.close()
        return [
            {"label": "Managed Compute", "cost": managed},
            {"label": "Serverless Compute", "cost": serverless},
            {"label": "AI & ML", "cost": ai_total},
            {"label": "Storage", "cost": storage},
            {"label": "Data Transfer", "cost": xfer},
            {"label": "Other", "cost": other},
        ]
    except Exception:  # noqa: BLE001
        return []


def top_warehouses_by_cost(limit: int = 10) -> pd.DataFrame:
    """Top N warehouses by total credit spend."""
    return _query(f"""
        SELECT warehouse_name AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE warehouse_id > 0
        GROUP BY warehouse_name
        HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
        LIMIT {limit}
    """)


def ai_spend_summary() -> dict[str, Any]:
    """AI spend KPIs — current month."""
    row_wh = _fetchone(
        "SELECT ROUND(SUM(credits_used), 0) "
        "FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
        "WHERE warehouse_name IN "
        "('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS') "
        "AND start_time >= DATE_TRUNC('month', CURRENT_DATE())"
    )
    row_svc = _fetchone(
        "SELECT ROUND(SUM(credits_used), 0) "
        "FROM ACCOUNT_USAGE.METERING_HISTORY "
        "WHERE start_time >= DATE_TRUNC('month', CURRENT_DATE()) "
        "AND service_type IN ('CORTEX_AI_FUNCTIONS','CORTEX_SEARCH',"
        "'AI_SERVICES','CORTEX_ANALYST','DOCUMENT_AI',"
        "'SNOWFLAKE_INTELLIGENCE','CORTEX_AGENTS','CORTEX_GUARDRAILS')"
    )
    wh = float((row_wh[0] if row_wh else 0) or 0)
    svc = float((row_svc[0] if row_svc else 0) or 0)
    total = wh + svc
    return {"ai_credits": total, "ai_cost": round(total * CREDIT_PRICE, 0)}


# ── Shadow Waste ──────────────────────────────────────────────────────────────

def hidden_waste_compute() -> pd.DataFrame:
    """Compute hidden waste: idle and oversized warehouses derived from ACCOUNT_USAGE."""
    return _query(f"""
        WITH metering AS (
            SELECT warehouse_name,
                   warehouse_size,
                   SUM(credits_used) AS credits_used
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE warehouse_id > 0
              AND start_time >= DATEADD('day', -30, CURRENT_DATE())
            GROUP BY warehouse_name, warehouse_size
        ),
        load AS (
            SELECT warehouse_name,
                   AVG(avg_running) AS avg_running
            FROM ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY
            WHERE start_time >= DATEADD('day', -30, CURRENT_DATE())
            GROUP BY warehouse_name
        )
        SELECT m.warehouse_name AS warehouse_name,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.10 THEN 'idle_running'
                    ELSE 'oversized' END AS waste_type,
               ROUND(m.credits_used * 24 / NULLIF(m.credits_used, 0)
                     * (1 - COALESCE(l.avg_running, 0)), 1) AS idle_hours,
               ROUND(m.credits_used * COALESCE(1 - l.avg_running, 0.5), 2)
                   AS wasted_credits,
               ROUND(m.credits_used * COALESCE(1 - l.avg_running, 0.5)
                     * {CREDIT_PRICE}, 2) AS wasted_cost_usd,
               ROUND(m.credits_used * {CREDIT_PRICE}, 2) AS actual_cost_usd,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.10
                    THEN 'Enable auto-suspend (60s); consider X-Small for dev'
                    ELSE 'Scale down warehouse size; review concurrent usage'
               END AS recommendation,
               COALESCE(m.warehouse_size, 'UNKNOWN') AS size
        FROM metering m
        LEFT JOIN load l USING (warehouse_name)
        WHERE m.credits_used >= 10
          AND COALESCE(l.avg_running, 0) < 0.50
        ORDER BY wasted_cost_usd DESC
    """)


def hidden_waste_storage() -> pd.DataFrame:
    """Storage hidden waste: stale tables with high TT / failsafe bytes."""
    return _query(f"""
        SELECT table_catalog || '.' || table_schema || '.' || table_name
               AS object_name,
               CASE WHEN failsafe_bytes > active_bytes THEN 'failsafe_excess'
                    WHEN time_travel_bytes > active_bytes THEN 'time_travel_excess'
                    ELSE 'stale_data' END AS waste_type,
               ROUND((time_travel_bytes + failsafe_bytes) / POWER(1024, 3), 2) AS size_gb,
               NULL AS days_since_access,
               ROUND((time_travel_bytes + failsafe_bytes)
                     / POWER(1024, 4) * {_STORAGE_COST_PER_TB}, 2) AS monthly_cost_usd,
               ROUND((time_travel_bytes + failsafe_bytes)
                     / POWER(1024, 4) * {_STORAGE_COST_PER_TB}, 2) AS actual_cost_usd,
               'Review data retention policy; shorten time-travel window'
               AS recommendation
        FROM ACCOUNT_USAGE.TABLE_STORAGE_METRICS
        WHERE (time_travel_bytes + failsafe_bytes) > POWER(1024, 3)
        ORDER BY (time_travel_bytes + failsafe_bytes) DESC
        LIMIT 50
    """)


def hidden_waste_ai() -> pd.DataFrame:
    """AI/Cortex hidden waste — returns empty DataFrame; no live signal available."""
    return pd.DataFrame(columns=[
        "waste_pattern", "function_name", "model_name", "task_type",
        "calls_30d", "actual_cost_usd", "wasted_credits", "wasted_cost_usd",
        "recommendation",
    ])


def hidden_waste_summary() -> dict[str, Any]:
    """Total hidden waste across all pillars."""
    try:
        compute_df = hidden_waste_compute()
        compute = float(compute_df["wasted_cost_usd"].sum()) if not compute_df.empty else 0.0

        storage_df = hidden_waste_storage()
        storage = float(storage_df["monthly_cost_usd"].sum() * 12) \
            if not storage_df.empty else 0.0

        row_30d = _fetchone(
            "SELECT COALESCE(SUM(credits_used), 0) "
            "FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY "
            "WHERE start_time >= DATEADD('day', -30, CURRENT_DATE())"
        )
        svc_30d = _fetchone(
            "SELECT COALESCE(SUM(credits_used), 0) "
            "FROM ACCOUNT_USAGE.METERING_HISTORY "
            "WHERE start_time >= DATEADD('day', -30, CURRENT_DATE())"
        )
        spend_30d = (
            float((row_30d[0] if row_30d else 0) or 0)
            + float((svc_30d[0] if svc_30d else 0) or 0)
        ) * CREDIT_PRICE
        total = round(compute + storage, 0)
        return {
            "compute": round(compute, 0),
            "storage_annual": round(storage, 0),
            "ai": 0.0,
            "total": total,
            "spend_30d": round(spend_30d, 0),
            "waste_pct": round(total / max(spend_30d, 1) * 100, 1),
        }
    except Exception:  # noqa: BLE001
        return {"compute": 0, "storage_annual": 0, "ai": 0, "total": 0,
                "spend_30d": 0, "waste_pct": 0.0}


# ── TCO Trend & Forecast ──────────────────────────────────────────────────────

def tco_monthly_trend_and_forecast() -> pd.DataFrame:
    """Monthly TCO — actuals for complete months, linear-regression forecast for 6 ahead."""
    df = _query("""
        WITH wh AS (
            SELECT DATE_TRUNC('month', start_time) AS month,
                   SUM(credits_used) AS credits
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE start_time >= DATEADD('month', -6, DATE_TRUNC('month', CURRENT_DATE()))
              AND start_time < DATE_TRUNC('month', CURRENT_DATE())
            GROUP BY DATE_TRUNC('month', start_time)
        ),
        svc AS (
            SELECT DATE_TRUNC('month', start_time) AS month,
                   SUM(credits_used) AS credits
            FROM ACCOUNT_USAGE.METERING_HISTORY
            WHERE start_time >= DATEADD('month', -6, DATE_TRUNC('month', CURRENT_DATE()))
              AND start_time < DATE_TRUNC('month', CURRENT_DATE())
            GROUP BY DATE_TRUNC('month', start_time)
        )
        SELECT COALESCE(w.month, s.month) AS month,
               COALESCE(w.credits, 0) + COALESCE(s.credits, 0) AS total_credits
        FROM wh w
        FULL OUTER JOIN svc s ON w.month = s.month
        ORDER BY month
    """)
    if df.empty:
        return pd.DataFrame()
    if "total_credits" not in df.columns or "month" not in df.columns:
        # Defensive: older connector paths or unexpected SELECT aliases.
        return pd.DataFrame()
    st = _fetchone(
        "SELECT ROUND((storage_bytes + stage_bytes + failsafe_bytes) "
        "/ POWER(1024, 4), 2) FROM ACCOUNT_USAGE.STORAGE_USAGE "
        "ORDER BY usage_date DESC LIMIT 1"
    )
    storage_tb = float((st[0] if st else 0) or 0)
    storage_monthly = storage_tb * _STORAGE_COST_PER_TB
    df["tco"] = df["total_credits"].astype(float) * CREDIT_PRICE + storage_monthly
    df["month"] = pd.to_datetime(df["month"])
    days_in_month = df["month"].dt.days_in_month
    df["tco"] = df["tco"] * (30.0 / days_in_month)
    df["type"] = "Actual"
    if len(df) >= 2:
        x = np.arange(len(df)).astype(float)
        y = df["tco"].values.astype(float)
        slope, intercept = np.polyfit(x, y, 1)
        forecast_rows = []
        for i in range(7):
            future_month = df["month"].max() + pd.DateOffset(months=i + 1)
            projected = intercept + slope * (len(df) - 1 + i + 1)
            forecast_rows.append({
                "month": future_month, "total_credits": 0,
                "tco": max(float(projected), 0), "type": "Forecast",
            })
        df = pd.concat([df, pd.DataFrame(forecast_rows)], ignore_index=True)
    return df


def tco_by_month() -> dict[date, float]:
    """Complete-month ACCOUNT_USAGE TCO keyed by month-start date.

    Same Actual series as ``tco_monthly_trend_and_forecast`` — not the as-of
    ``kpi_summary`` window.
    """
    df = tco_monthly_trend_and_forecast()
    if df.empty:
        return {}
    actual = df[df["type"] == "Actual"] if "type" in df.columns else df
    out: dict[date, float] = {}
    for _, row in actual.iterrows():
        month = pd.Timestamp(row["month"]).date().replace(day=1)
        out[month] = float(row["tco"])
    return out


# ── Warehouse & Compute ───────────────────────────────────────────────────────

def warehouse_cost_profile() -> pd.DataFrame:
    """Top warehouses — credits + load metrics."""
    return _query(f"""
        WITH m AS (
            SELECT warehouse_name, SUM(credits_used) AS credits
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY WHERE warehouse_id > 0
            GROUP BY warehouse_name
        ),
        l AS (
            SELECT warehouse_name,
                   AVG(avg_running) AS avg_running,
                   AVG(avg_queued_load) AS avg_queued_load
            FROM ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY GROUP BY warehouse_name
        )
        SELECT m.warehouse_name AS object_name,
               ROUND(m.credits, 2) AS credits,
               ROUND(m.credits * {CREDIT_PRICE}, 2) AS cost_usd,
               ROUND(COALESCE(l.avg_running, 0) * 100, 1) AS avg_running_pct,
               ROUND(COALESCE(l.avg_queued_load, 0), 3) AS avg_queued_load
        FROM m LEFT JOIN l USING (warehouse_name)
        WHERE m.credits > 0
        ORDER BY m.credits DESC
    """)


def warehouse_load_profile() -> pd.DataFrame:
    """Warehouse load history (last 7 days)."""
    return _query("""
        SELECT CAST(start_time AS DATE) AS date,
               warehouse_name AS object_name,
               ROUND(AVG(avg_running), 3) AS avg_running,
               ROUND(AVG(avg_queued_load), 3) AS avg_queued_load
        FROM ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY
        WHERE start_time >= DATEADD('day', -7, CURRENT_DATE())
        GROUP BY CAST(start_time AS DATE), warehouse_name
        ORDER BY date, warehouse_name
    """)


def warehouse_heatmap() -> pd.DataFrame:
    """Daily credits by warehouse for the last 21 days."""
    return _query(f"""
        WITH wh AS (
            SELECT warehouse_name,
                   CAST(start_time AS DATE) AS usage_date,
                   SUM(credits_used) AS daily_credits
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE start_time >= DATEADD('day', -21, CURRENT_DATE())
              AND warehouse_id > 0
            GROUP BY warehouse_name, CAST(start_time AS DATE)
        ),
        top_wh AS (
            SELECT warehouse_name FROM wh
            GROUP BY warehouse_name ORDER BY SUM(daily_credits) DESC LIMIT 10
        )
        SELECT w.warehouse_name AS object_name, w.usage_date AS date,
               ROUND(w.daily_credits * {CREDIT_PRICE}, 2) AS cost_usd
        FROM wh w JOIN top_wh t ON w.warehouse_name = t.warehouse_name
        ORDER BY t.warehouse_name, w.usage_date
    """)


def idle_warehouses() -> pd.DataFrame:
    """Idle or underused warehouses."""
    return _query(f"""
        WITH m AS (
            SELECT warehouse_name, SUM(credits_used) AS credits_used
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY WHERE warehouse_id > 0
            GROUP BY warehouse_name
        ),
        l AS (
            SELECT warehouse_name, AVG(avg_running) AS avg_running,
                   AVG(avg_queued_load) AS avg_queued_load
            FROM ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY GROUP BY warehouse_name
        )
        SELECT m.warehouse_name AS object_name,
               ROUND(m.credits_used, 2) AS credits,
               ROUND(m.credits_used * {CREDIT_PRICE}, 2) AS cost_usd,
               ROUND(COALESCE(l.avg_running, 0), 3) AS avg_running,
               ROUND(COALESCE(l.avg_queued_load, 0), 3) AS avg_queued_load,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.15 AND m.credits_used >= 100
                    THEN 'BLOCKER'
                    WHEN COALESCE(l.avg_running, 0) < 0.30 AND m.credits_used >= 25
                    THEN 'WARN'
                    ELSE 'INFO' END AS severity
        FROM m LEFT JOIN l USING (warehouse_name)
        WHERE m.credits_used >= 10
        ORDER BY m.credits_used DESC
    """)


def warehouse_cost_efficiency() -> pd.DataFrame:
    """Warehouse credits per scanned TB and per 1000 queries."""
    return _query("""
        SELECT warehouse_name AS object_name,
               ROUND(SUM(bytes_scanned) / POWER(1024, 4), 2) AS scanned_tb,
               COUNT(*) AS queries,
               ROUND(AVG(COALESCE(percentage_scanned_from_cache, 0)), 1) AS avg_cache_pct,
               ROUND(SUM(bytes_spilled_to_remote_storage) / POWER(1024, 3), 2)
                   AS spilled_gb
        FROM ACCOUNT_USAGE.QUERY_HISTORY
        WHERE execution_status = 'SUCCESS' AND warehouse_name IS NOT NULL
        GROUP BY warehouse_name
        HAVING COUNT(*) > 0
        ORDER BY SUM(bytes_scanned) DESC
        LIMIT 30
    """)


def queue_pressure() -> pd.DataFrame:
    """Warehouse queue pressure."""
    return _query("""
        SELECT warehouse_name AS object_name,
               ROUND(SUM(queued_overload_time) / 1000.0, 1) AS queued_seconds,
               COUNT(*) AS queries
        FROM ACCOUNT_USAGE.QUERY_HISTORY
        WHERE warehouse_name IS NOT NULL
        GROUP BY warehouse_name
        HAVING SUM(queued_overload_time) > 0
        ORDER BY SUM(queued_overload_time) DESC
    """)


# ── Queries ───────────────────────────────────────────────────────────────────

def query_performance_profile() -> pd.DataFrame:
    """Top expensive query patterns by scan + elapsed time."""
    return _query("""
        SELECT query_hash AS object_name,
               COUNT(*) AS queries,
               ROUND(SUM(bytes_scanned) / POWER(1024, 4), 2) AS tb_scanned,
               ROUND(SUM(bytes_spilled_to_remote_storage) / POWER(1024, 3), 2)
                   AS gb_remote_spill,
               ROUND(MAX(total_elapsed_time) / 1000.0, 1) AS max_elapsed_sec,
               ANY_VALUE(warehouse_name) AS warehouse
        FROM ACCOUNT_USAGE.QUERY_HISTORY
        WHERE execution_status = 'SUCCESS' AND query_hash IS NOT NULL
        GROUP BY query_hash
        HAVING COUNT(*) >= 3
          AND (SUM(bytes_scanned) > POWER(1024, 4)
               OR SUM(bytes_spilled_to_remote_storage) > 0
               OR MAX(total_elapsed_time) > 300000)
        ORDER BY SUM(bytes_scanned) DESC
        LIMIT 50
    """)


def expensive_query_patterns() -> pd.DataFrame:
    return query_performance_profile()


def cache_reuse_opportunity() -> pd.DataFrame:
    """Recurring patterns with poor cache reuse."""
    return _query("""
        SELECT COALESCE(query_parameterized_hash, query_hash) AS object_name,
               COUNT(*) AS queries,
               ROUND(SUM(bytes_scanned) / POWER(1024, 4), 2) AS tb_scanned,
               ROUND(AVG(COALESCE(percentage_scanned_from_cache, 0)), 1) AS avg_cache_pct,
               COUNT(DISTINCT warehouse_name) AS warehouses
        FROM ACCOUNT_USAGE.QUERY_HISTORY
        WHERE execution_status = 'SUCCESS' AND query_type = 'SELECT'
          AND COALESCE(query_parameterized_hash, query_hash) IS NOT NULL
        GROUP BY COALESCE(query_parameterized_hash, query_hash)
        HAVING COUNT(*) >= 5
           AND AVG(COALESCE(percentage_scanned_from_cache, 0)) < 40
        ORDER BY SUM(bytes_scanned) DESC
        LIMIT 50
    """)


def top_users_hidden_waste(top_n: int = 5) -> pd.DataFrame:
    """Top users contributing to hidden waste — simplified live version."""
    return pd.DataFrame()


# ── Storage ───────────────────────────────────────────────────────────────────

def storage_trend() -> pd.DataFrame:
    """Account storage trend."""
    return _query("""
        SELECT usage_date AS date,
               ROUND(storage_bytes / POWER(1024, 4), 3) AS table_tb,
               ROUND(stage_bytes / POWER(1024, 4), 3) AS stage_tb,
               ROUND(failsafe_bytes / POWER(1024, 4), 3) AS failsafe_tb,
               0.0 AS hybrid_tb,
               0.0 AS archive_cool_tb,
               0.0 AS archive_cold_tb
        FROM ACCOUNT_USAGE.STORAGE_USAGE
        ORDER BY usage_date
    """)


def top_storage_tables(limit: int = 30) -> pd.DataFrame:
    """Largest tables by total storage."""
    return _query(f"""
        SELECT table_catalog || '.' || table_schema || '.' || table_name AS object_name,
               ROUND(active_bytes / POWER(1024, 3), 2) AS active_gb,
               ROUND(time_travel_bytes / POWER(1024, 3), 2) AS time_travel_gb,
               ROUND(failsafe_bytes / POWER(1024, 3), 2) AS failsafe_gb,
               ROUND(retained_for_clone_bytes / POWER(1024, 3), 2) AS clone_gb,
               is_transient AS transient
        FROM ACCOUNT_USAGE.TABLE_STORAGE_METRICS
        WHERE (active_bytes + time_travel_bytes + failsafe_bytes) > 0
        ORDER BY (active_bytes + time_travel_bytes + failsafe_bytes
                  + retained_for_clone_bytes) DESC
        LIMIT {limit}
    """)


def top_tables() -> pd.DataFrame:
    return top_storage_tables()


def top_tables_storage(limit: int = 25) -> pd.DataFrame:
    return top_storage_tables(limit)


# ── Governance ────────────────────────────────────────────────────────────────

def unattributed_spend() -> pd.DataFrame:
    """Warehouses missing owner/cost-center tags."""
    return _query(f"""
        WITH wh_spend AS (
            SELECT warehouse_name, SUM(credits_used) AS credits_used
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY WHERE warehouse_id > 0
            GROUP BY warehouse_name
        ),
        tag_refs AS (
            SELECT object_name AS warehouse_name,
                   MAX(CASE WHEN tag_name IN ('owner', 'business_owner', 'team')
                       THEN tag_value END) AS owner_tag,
                   MAX(CASE WHEN tag_name IN ('cost_center', 'costcenter')
                       THEN tag_value END) AS cost_center_tag
            FROM ACCOUNT_USAGE.TAG_REFERENCES WHERE domain = 'WAREHOUSE'
            GROUP BY object_name
        )
        SELECT s.warehouse_name AS object_name,
               ROUND(s.credits_used, 2) AS credits,
               ROUND(s.credits_used * {CREDIT_PRICE}, 2) AS cost_usd,
               COALESCE(t.owner_tag, '<missing>') AS owner,
               COALESCE(t.cost_center_tag, '<missing>') AS cost_center
        FROM wh_spend s LEFT JOIN tag_refs t USING (warehouse_name)
        WHERE t.owner_tag IS NULL OR t.cost_center_tag IS NULL
        ORDER BY s.credits_used DESC
    """)


# ── Spend helpers ─────────────────────────────────────────────────────────────

def spend_by_warehouse() -> pd.DataFrame:
    return _query(f"""
        SELECT warehouse_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY WHERE warehouse_id > 0
        GROUP BY warehouse_name ORDER BY SUM(credits_used) DESC
    """)


def spend_by_day(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    where = "WHERE warehouse_id > 0"
    if start:
        where += f" AND CAST(start_time AS DATE) >= '{start}'"
    if end:
        where += f" AND CAST(start_time AS DATE) <= '{end}'"
    return _query(f"""
        SELECT CAST(start_time AS DATE) AS date,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        {where}
        GROUP BY CAST(start_time AS DATE) ORDER BY date
    """)


def idle_warehouses_filtered(start: str, end: str) -> pd.DataFrame:
    return _query(f"""
        WITH m AS (
            SELECT warehouse_name, SUM(credits_used) AS credits_used
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
              AND warehouse_id > 0
            GROUP BY warehouse_name
        ),
        l AS (
            SELECT warehouse_name,
                   AVG(avg_running) AS avg_running,
                   AVG(avg_queued_load) AS avg_queued_load
            FROM ACCOUNT_USAGE.WAREHOUSE_LOAD_HISTORY
            WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
            GROUP BY warehouse_name
        )
        SELECT m.warehouse_name AS object_name,
               ROUND(m.credits_used, 2) AS credits,
               ROUND(m.credits_used * {CREDIT_PRICE}, 2) AS cost_usd,
               ROUND(COALESCE(l.avg_running, 0), 3) AS avg_running,
               ROUND(COALESCE(l.avg_queued_load, 0), 3) AS avg_queued_load,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.15 AND m.credits_used >= 100
                    THEN 'BLOCKER'
                    WHEN COALESCE(l.avg_running, 0) < 0.30 AND m.credits_used >= 25
                    THEN 'WARN'
                    ELSE 'INFO' END AS severity
        FROM m LEFT JOIN l USING (warehouse_name) WHERE m.credits_used >= 10
        ORDER BY m.credits_used DESC
    """)


def queue_pressure_filtered(start: str, end: str) -> pd.DataFrame:
    return _query(f"""
        SELECT warehouse_name AS object_name,
               ROUND(SUM(queued_overload_time) / 1000.0, 1) AS queued_seconds,
               COUNT(*) AS queries
        FROM ACCOUNT_USAGE.QUERY_HISTORY
        WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
          AND warehouse_name IS NOT NULL
        GROUP BY warehouse_name HAVING SUM(queued_overload_time) > 0
        ORDER BY SUM(queued_overload_time) DESC
    """)


def warehouse_daily_credits(top_n: int = 10) -> pd.DataFrame:
    return _query(f"""
        WITH wh AS (
            SELECT warehouse_name, CAST(start_time AS DATE) AS usage_date,
                   SUM(credits_used) AS daily_credits
            FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
            WHERE start_time >= DATEADD('day', -21, CURRENT_DATE()) AND warehouse_id > 0
            GROUP BY warehouse_name, CAST(start_time AS DATE)
        ),
        top AS (
            SELECT warehouse_name FROM wh
            GROUP BY warehouse_name ORDER BY SUM(daily_credits) DESC LIMIT {top_n}
        )
        SELECT w.warehouse_name, w.usage_date, w.daily_credits
        FROM wh w JOIN top t ON w.warehouse_name = t.warehouse_name
        ORDER BY t.warehouse_name, w.usage_date
    """)


def warehouse_spend_filtered(start: str, end: str, rollup: str = "day") -> pd.DataFrame:
    trunc = {"day": "day", "week": "week", "month": "month", "year": "year"}[rollup]
    return _query(f"""
        SELECT DATE_TRUNC('{trunc}', start_time) AS period,
               warehouse_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}' AND warehouse_id > 0
        GROUP BY period, warehouse_name ORDER BY period, SUM(credits_used) DESC
    """)


def warehouse_summary_filtered(start: str, end: str) -> pd.DataFrame:
    return _query(f"""
        SELECT warehouse_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}' AND warehouse_id > 0
        GROUP BY warehouse_name ORDER BY SUM(credits_used) DESC
    """)


def top_users_daily_credits(top_n: int = 10, user_type: str = "all") -> pd.DataFrame:
    _svc = "('ETL_SERVICE','ML_PIPELINE','DBT_RUNNER')"
    if user_type == "service":
        uf = f"AND (user_name LIKE '%_SVC' OR user_name IN {_svc})"
    elif user_type == "adhoc":
        uf = f"AND user_name NOT LIKE '%_SVC' AND user_name NOT IN {_svc}"
    else:
        uf = ""
    return _query(f"""
        WITH uc AS (
            SELECT user_name, CAST(start_time AS DATE) AS usage_date,
                   COUNT(*) AS daily_credits
            FROM ACCOUNT_USAGE.QUERY_HISTORY
            WHERE start_time >= DATEADD('day', -21, CURRENT_DATE())
              AND execution_status = 'SUCCESS' {uf}
            GROUP BY user_name, CAST(start_time AS DATE)
        ),
        top AS (
            SELECT user_name FROM uc GROUP BY user_name
            ORDER BY SUM(daily_credits) DESC LIMIT {top_n}
        )
        SELECT uc.user_name, uc.usage_date, uc.daily_credits
        FROM uc JOIN top t ON uc.user_name = t.user_name
        ORDER BY t.user_name, uc.usage_date
    """)


def query_attributed_cost() -> pd.DataFrame:
    return _query("""
        SELECT query_parameterized_hash AS object_name,
               COUNT(*) AS queries,
               ROUND(SUM(bytes_scanned) / POWER(1024, 4), 2) AS tb_scanned,
               ANY_VALUE(warehouse_name) AS warehouse
        FROM ACCOUNT_USAGE.QUERY_HISTORY
        WHERE query_parameterized_hash IS NOT NULL AND execution_status = 'SUCCESS'
        GROUP BY query_parameterized_hash
        HAVING COUNT(*) >= 2
        ORDER BY SUM(bytes_scanned) DESC
        LIMIT 50
    """)


def top_tables_storage_filtered(start: str, end: str) -> pd.DataFrame:
    return top_storage_tables()


# ── AI / Cortex ───────────────────────────────────────────────────────────────

def ai_service_metering() -> pd.DataFrame:
    return _query(f"""
        SELECT service_type || ':' || name AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd,
               ANY_VALUE(entity_type) AS entity_type
        FROM ACCOUNT_USAGE.METERING_HISTORY
        GROUP BY service_type, name HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
    """)


def ai_function_usage() -> pd.DataFrame:
    return _query(f"""
        SELECT function_name || ':' || model_name AS object_name,
               ROUND(SUM(credits), 2) AS credits,
               ROUND(SUM(credits) * {CREDIT_PRICE}, 2) AS cost_usd,
               SUM(calls) AS calls,
               SUM(tokens_sent) AS tokens_sent,
               SUM(tokens_received) AS tokens_received,
               COUNT(DISTINCT user_id) AS users
        FROM ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY
        GROUP BY function_name, model_name HAVING SUM(credits) > 0
        ORDER BY SUM(credits) DESC
    """)


def ai_search_daily() -> pd.DataFrame:
    return _query(f"""
        SELECT database_name || '.' || schema_name || '.' || service_name AS object_name,
               consumption_type,
               ROUND(SUM(credits), 2) AS credits,
               ROUND(SUM(credits) * {CREDIT_PRICE}, 2) AS cost_usd,
               SUM(tokens) AS tokens
        FROM ACCOUNT_USAGE.CORTEX_SEARCH_DAILY_USAGE_HISTORY
        GROUP BY database_name, schema_name, service_name, consumption_type
        HAVING SUM(credits) > 0
        ORDER BY SUM(credits) DESC
    """)


def ai_spend_by_day() -> pd.DataFrame:
    return _query(f"""
        SELECT CAST(start_time AS DATE) AS date,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.METERING_HISTORY
        GROUP BY CAST(start_time AS DATE) ORDER BY date
    """)


def ai_cost_breakdown() -> list[dict[str, Any]]:
    df = _query("""
        SELECT service_type, SUM(credits_used) AS credits
        FROM ACCOUNT_USAGE.METERING_HISTORY
        WHERE service_type IN ('CORTEX_AI_FUNCTIONS','CORTEX_SEARCH',
            'AI_SERVICES','CORTEX_ANALYST','DOCUMENT_AI',
            'SNOWFLAKE_INTELLIGENCE','CORTEX_AGENTS','CORTEX_GUARDRAILS')
        GROUP BY service_type ORDER BY credits DESC
    """)
    results = []
    if not df.empty:
        for _, row in df.iterrows():
            label = str(row["service_type"]).replace("_", " ").title()
            results.append({"label": label, "cost": float(row["credits"]) * CREDIT_PRICE})
    return results


def serverless_cost_breakdown() -> list[dict[str, Any]]:
    df = _query("""
        SELECT service_type, SUM(credits_used) AS credits
        FROM ACCOUNT_USAGE.METERING_HISTORY
        WHERE service_type IN ('AUTOMATIC_CLUSTERING','SNOWPIPE','SERVERLESS_TASK',
            'REPLICATION','DATA_TRANSFER','SEARCH_OPTIMIZATION',
            'MATERIALIZED_VIEW','QUERY_ACCELERATION','SNOWPARK_CONTAINER_SERVICES')
        GROUP BY service_type ORDER BY credits DESC
    """)
    results = []
    if not df.empty:
        for _, row in df.iterrows():
            label = str(row["service_type"]).replace("_", " ").title()
            results.append({"label": label, "cost": float(row["credits"]) * CREDIT_PRICE})
    return results


# ── Serverless Services ───────────────────────────────────────────────────────

def serverless_optimization_spend() -> pd.DataFrame:
    return _query(f"""
        SELECT database_name || '.' || schema_name || '.' || table_name AS object_name,
               'AUTOMATIC_CLUSTERING' AS service_type,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.AUTOMATIC_CLUSTERING_HISTORY
        GROUP BY database_name, schema_name, table_name
        HAVING SUM(credits_used) > 0 ORDER BY SUM(credits_used) DESC
    """)


def snowpipe_cost() -> pd.DataFrame:
    return _query(f"""
        SELECT pipe_name AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd,
               ROUND(SUM(bytes_inserted) / POWER(1024, 3), 2) AS gb_inserted,
               SUM(files_inserted) AS files
        FROM ACCOUNT_USAGE.PIPE_USAGE_HISTORY
        GROUP BY pipe_name HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
    """)


def serverless_task_costs() -> pd.DataFrame:
    return _query(f"""
        SELECT database_name || '.' || schema_name || '.' || task_name AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.SERVERLESS_TASK_HISTORY
        GROUP BY database_name, schema_name, task_name
        HAVING SUM(credits_used) > 0 ORDER BY SUM(credits_used) DESC
    """)


def data_transfer_drivers() -> pd.DataFrame:
    return _query("""
        SELECT transfer_type || ':' || source_cloud || '/' || source_region
               || '->' || target_cloud || '/' || target_region AS object_name,
               ROUND(SUM(bytes_transferred) / POWER(1024, 4), 3) AS tb_transferred
        FROM ACCOUNT_USAGE.DATA_TRANSFER_HISTORY
        GROUP BY transfer_type, source_cloud, source_region, target_cloud, target_region
        HAVING SUM(bytes_transferred) > 0
        ORDER BY SUM(bytes_transferred) DESC
    """)


def executive_cost_trend() -> pd.DataFrame:
    return _query(f"""
        SELECT service_type AS object_name,
               usage_date AS date,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.METERING_DAILY_HISTORY
        GROUP BY usage_date, service_type ORDER BY usage_date, SUM(credits_used) DESC
    """)


def all_service_cost_profile() -> pd.DataFrame:
    return _query(f"""
        SELECT service_type AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM ACCOUNT_USAGE.METERING_DAILY_HISTORY
        GROUP BY service_type HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
    """)


def cost_breakdown_monthly(months: int = 12) -> pd.DataFrame:
    """Monthly cost breakdown by service category for the last N months."""
    try:
        with _borrow_conn() as conn:
            if conn is None:
                return pd.DataFrame()
            cur = conn.cursor()
            try:
                cutoff = f"DATEADD(MONTH, -{months}, DATE_TRUNC('MONTH', CURRENT_DATE()))"

                # Warehouse metering
                cur.execute(f"""
                    SELECT DATE_TRUNC('MONTH', start_time) AS month,
                           CASE WHEN warehouse_name IN
                             ('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS')
                             THEN 'AI & ML' ELSE 'Managed Compute'
                           END AS category,
                           SUM(credits_used) * {CREDIT_PRICE} AS cost_usd
                    FROM ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
                    WHERE start_time >= {cutoff}
                    GROUP BY 1, 2
                """)
                wh = pd.DataFrame(
                    cur.fetchall(), columns=["month", "category", "cost_usd"]
                )

                # Serverless metering
                cur.execute(f"""
                    SELECT DATE_TRUNC('MONTH', start_time) AS month,
                           CASE
                             WHEN service_type IN (
                               'CORTEX_AI_FUNCTIONS','CORTEX_SEARCH','AI_SERVICES',
                               'CORTEX_ANALYST','DOCUMENT_AI','SNOWFLAKE_INTELLIGENCE',
                               'CORTEX_AGENTS','CORTEX_GUARDRAILS') THEN 'AI & ML'
                             WHEN service_type IN (
                               'AUTOMATIC_CLUSTERING','AUTO_CLUSTERING',
                               'SNOWPIPE','PIPE','SNOWPIPE_STREAMING',
                               'SERVERLESS_TASK','SERVERLESS_ALERTS',
                               'REPLICATION','SEARCH_OPTIMIZATION','MATERIALIZED_VIEW',
                               'QUERY_ACCELERATION','SNOWPARK_CONTAINER_SERVICES',
                               'HYBRID_TABLE_REQUESTS',
                               'WAREHOUSE_METERING','WAREHOUSE_METERING_READER')
                               THEN 'Serverless Compute'
                             WHEN service_type IN (
                               'FAILSAFE_RECOVERY',
                               'ARCHIVE_STORAGE_RETRIEVAL_FILE_PROCESSING',
                               'ARCHIVE_STORAGE_WRITE',
                               'STORAGE_LIFECYCLE_POLICY_EXECUTION') THEN 'Storage'
                             WHEN service_type = 'DATA_TRANSFER' THEN 'Data Transfer'
                             ELSE 'Other'
                           END AS category,
                           SUM(credits_used) * {CREDIT_PRICE} AS cost_usd
                    FROM ACCOUNT_USAGE.METERING_HISTORY
                    WHERE start_time >= {cutoff}
                    GROUP BY 1, 2
                """)
                svc = pd.DataFrame(
                    cur.fetchall(), columns=["month", "category", "cost_usd"]
                )

                # Storage
                cur.execute(f"""
                    SELECT DATE_TRUNC('MONTH', usage_date) AS month,
                           'Storage' AS category,
                           AVG((storage_bytes + stage_bytes + failsafe_bytes)
                               / POWER(1024, 4)) * 23.0 AS cost_usd
                    FROM ACCOUNT_USAGE.STORAGE_USAGE
                    WHERE usage_date >= {cutoff}
                    GROUP BY 1
                """)
                storage = pd.DataFrame(
                    cur.fetchall(), columns=["month", "category", "cost_usd"]
                )

                df = pd.concat([wh, svc, storage], ignore_index=True)
                if df.empty:
                    return pd.DataFrame()

                df = df.groupby(["month", "category"], as_index=False)["cost_usd"].sum()
                df["cost_usd"] = df["cost_usd"].round(2)
                df["month"] = pd.to_datetime(df["month"]).dt.strftime("%Y-%m")
                df = df.sort_values(["month", "category"])
                return df
            finally:
                cur.close()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
