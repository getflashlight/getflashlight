"""Snowflake visibility data access — DuckDB over local ACCOUNT_USAGE Parquet.

Primary source is ``FLASHLIGHT_HOME/account_usage/`` written by ingest (or
``flashlight sample``). Falls back to repo ``snowflake/synthetic_data/*.parquet`` for
local/dev when the lake is empty. The dashboard never opens a live Snowflake connection.

All "current month / last N days" windows are anchored to the latest
``warehouse_metering_history.start_time`` in the local dataset (not the wall clock).
"""

from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict

import duckdb
import pandas as pd

from flashlight.lake import account_usage as account_usage_lake
from flashlight.lake import paths

# visibility_data.py → snowflake/ → dashboard/ → flashlight/ → src/ → repo root
_SYNTHETIC_DIR = Path(__file__).resolve().parents[4] / "snowflake" / "synthetic_data"
# Match snowflake/synthetic_data/generate.py ($4/credit → ≤$600K/year demo).
CREDIT_PRICE = 4.00


class CostBreakdownItem(TypedDict):
    label: str
    cost: float


def has_local_data() -> bool:
    """Whether Visibility can render from lake ACCOUNT_USAGE or repo synthetic Parquet."""
    if account_usage_lake.has_parquet():
        return True
    return any(_SYNTHETIC_DIR.glob("*.parquet"))


def has_synthetic_data() -> bool:
    """Backward-compatible alias for :func:`has_local_data`."""
    return has_local_data()


def _table_paths(table_name: str) -> list[Path]:
    """Parquet files for a view stem — lake first (exclusive), else repo synthetic."""
    if account_usage_lake.has_parquet():
        return account_usage_lake.table_parquet_paths(table_name)
    synth = _SYNTHETIC_DIR / f"{table_name}.parquet"
    return [synth] if synth.exists() else []


def _known_table_stems() -> set[str]:
    """Discover table stems from lake (preferred) or repo synthetic fallback."""
    stems: set[str] = set()
    root = paths.account_usage_dir()
    if root.exists():
        for p in root.glob("provider_name=*/"):
            for table_dir in p.iterdir():
                if table_dir.is_dir():
                    stems.add(table_dir.name.lower())
        for pf in root.glob("*.parquet"):
            stems.add(pf.stem.lower())
    if stems:
        return stems
    for pf in _SYNTHETIC_DIR.glob("*.parquet"):
        stems.add(pf.stem.lower())
    return stems


def _has_table(table_name: str) -> bool:
    return bool(_table_paths(table_name))


def _has_column(table_name: str, column: str) -> bool:
    """Whether ``column`` exists on the registered Parquet for ``table_name``."""
    files = _table_paths(table_name)
    if not files:
        return False
    import pyarrow.parquet as pq

    names = {str(f.name).lower() for f in pq.read_schema(files[0])}
    return column.lower() in names


def _con() -> duckdb.DuckDBPyConnection:
    """Connect and register each ACCOUNT_USAGE table as a DuckDB view."""
    con = duckdb.connect()
    hive_cols = {"provider_name", "charge_month"}
    for stem in sorted(_known_table_stems()):
        files = _table_paths(stem)
        if not files:
            continue
        if len(files) == 1:
            src = f"read_parquet('{files[0]}', hive_partitioning=false)"
        else:
            listed = ", ".join(f"'{p}'" for p in files)
            src = f"read_parquet([{listed}], union_by_name=true, hive_partitioning=false)"
        # Probe columns, then CREATE VIEW with lowercased aliases so SQL matches
        # the synthetic demo shape even if a dump kept Snowflake UPPERCASE names.
        probe = con.execute(f"SELECT * FROM {src} LIMIT 0")
        cols = [desc[0] for desc in probe.description] if probe.description else []
        cols = [c for c in cols if str(c).lower() not in hive_cols]
        if not cols:
            con.execute(f"CREATE VIEW {stem} AS SELECT * FROM {src}")  # noqa: S608
            continue
        select_list = ", ".join(f'"{c}" AS {str(c).lower()}' for c in cols)
        con.execute(f"CREATE VIEW {stem} AS SELECT {select_list} FROM {src}")  # noqa: S608
    return con


@lru_cache(maxsize=1)
def _as_of() -> date:
    """Latest day present in the local warehouse metering history.

    Falls back to today only if the Parquet set is missing/empty.
    """
    files = _table_paths("warehouse_metering_history")
    if not files:
        return date.today()
    con = _con()
    try:
        row = con.execute(
            "SELECT MAX(start_time)::DATE FROM warehouse_metering_history"
        ).fetchone()
        if not row or row[0] is None:
            return date.today()
        value = row[0]
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value)[:10])
    finally:
        con.close()


def _as_of_sql() -> str:
    """DuckDB date literal for :func:`_as_of` — drop-in for ``CURRENT_DATE``."""
    return f"DATE '{_as_of().isoformat()}'"


def _query(sql: str) -> pd.DataFrame:
    con = _con()
    try:
        return _coerce_decimal_columns(con.execute(sql).df())
    finally:
        con.close()


def _as_float(value: object, default: float = 0.0) -> float:
    """Coerce DuckDB/Snowflake Decimals (and None) to float for Python arithmetic."""
    if value is None:
        return default
    if isinstance(value, (bool, int, float, str)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    # Decimal and other SupportsFloat-ish objects from the connector.
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _coerce_decimal_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Turn object-dtype Decimal columns into float64 so ``CREDIT_PRICE`` math works."""
    from decimal import Decimal

    if df.empty:
        return df
    out = df
    for col in out.columns:
        sample = out[col].dropna().head(1)
        if sample.empty:
            continue
        if isinstance(sample.iloc[0], Decimal):
            out = out.copy()
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


def _as_naive_timestamp(series: pd.Series) -> pd.Series:
    """Normalize mixed tz-aware / tz-naive timestamps to naive UTC.

    Live Snowflake ACCOUNT_USAGE dumps often carry TIMESTAMP_LTZ (tz-aware); demo
    Parquet and DATE columns are naive. Pandas cannot groupby or compare the mix.
    """
    ts = pd.to_datetime(series, utc=True, errors="coerce")
    return ts.dt.tz_localize(None)


# ── Overview KPIs ──────────────────────────────────────────────────────────────

def kpi_summary() -> dict[str, Any]:
    """Top-line KPIs for the overview tab. Total includes serverless AI."""
    as_of = _as_of_sql()
    as_of_date = _as_of()
    con = _con()
    try:
        # Monthly cost: latest month present in the synthetic dataset
        wh = con.execute(
            "SELECT SUM(credits_used) FROM warehouse_metering_history "
            f"WHERE start_time >= DATE_TRUNC('month', {as_of})"
        ).fetchone()
        wh_credits = _as_float(wh[0] if wh else None)
        svc = con.execute(
            "SELECT SUM(credits_used) FROM metering_history "
            f"WHERE start_time >= DATE_TRUNC('month', {as_of})"
        ).fetchone()
        svc_credits = _as_float(svc[0] if svc else None)
        total_credits = wh_credits + svc_credits
        # AI warehouse credits (to subtract from compute for non-overlapping tiles)
        ai_wh = con.execute(
            "SELECT COALESCE(SUM(credits_used), 0) FROM warehouse_metering_history "
            "WHERE warehouse_name IN "
            "('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS') "
            f"AND start_time >= DATE_TRUNC('month', {as_of})"
        ).fetchone()
        ai_wh_credits = _as_float(ai_wh[0] if ai_wh else None)
        # Serverless compute credits (non-AI managed services)
        svl = con.execute(
            "SELECT COALESCE(SUM(credits_used), 0) FROM metering_history "
            f"WHERE start_time >= DATE_TRUNC('month', {as_of}) "
            "AND service_type IN ('AUTOMATIC_CLUSTERING','SNOWPIPE','SERVERLESS_TASK',"
            "'REPLICATION','SEARCH_OPTIMIZATION','MATERIALIZED_VIEW','QUERY_ACCELERATION',"
            "'SNOWPARK_CONTAINER_SERVICES')"
        ).fetchone()
        serverless_credits = _as_float(svl[0] if svl else None)
        warehouses = con.execute(
            "SELECT COUNT(DISTINCT warehouse_name) FROM warehouse_metering_history"
        ).fetchone()
        q = con.execute(
            "SELECT COUNT(*) FROM query_history WHERE execution_status = 'SUCCESS'"
        ).fetchone()
        u = con.execute(
            "SELECT AVG(avg_running) FROM warehouse_load_history"
        ).fetchone()
        s = con.execute(
            "SELECT ROUND((storage_bytes + stage_bytes) / POWER(1024, 4), 2) "
            "FROM storage_usage ORDER BY usage_date DESC LIMIT 1"
        ).fetchone()
        # Storage cost: $23/TB/month (Snowflake on-demand)
        storage_tb = _as_float(s[0] if s else None)
        storage_cost = round(storage_tb * 23, 0)
        # YTD spend: sum all credits consumed in the as-of year
        ytd = con.execute(
            "SELECT COALESCE(SUM(credits_used), 0) "
            "FROM warehouse_metering_history "
            f"WHERE start_time >= DATE_TRUNC('year', {as_of})"
        ).fetchone()
        ytd_svc = con.execute(
            "SELECT COALESCE(SUM(credits_used), 0) "
            "FROM metering_history "
            f"WHERE start_time >= DATE_TRUNC('year', {as_of})"
        ).fetchone()
        ytd_credits = _as_float(ytd[0] if ytd else None) + _as_float(
            ytd_svc[0] if ytd_svc else None
        )
        month_label = as_of_date.strftime("%B %Y")
        months_ytd = as_of_date.month  # months elapsed this year (including current)
        # Compute cost: warehouse credits excluding AI warehouses (current month)
        compute_cost = round((wh_credits - ai_wh_credits) * CREDIT_PRICE, 0)
        serverless_compute_cost = round(serverless_credits * CREDIT_PRICE, 0)
        return {
            "total_credits": round(total_credits, 0),
            "total_cost": round(total_credits * CREDIT_PRICE + storage_cost, 0),
            "compute_cost": compute_cost,
            "serverless_compute_cost": serverless_compute_cost,
            "month_label": month_label,
            "warehouses": int(_as_float(warehouses[0] if warehouses else None)),
            "queries": int(_as_float(q[0] if q else None)),
            "avg_utilization_pct": round(_as_float(u[0] if u else None) * 100, 1),
            "storage_tb": storage_tb,
            "storage_cost": storage_cost,
            "ytd_cost": round(ytd_credits * CREDIT_PRICE + storage_cost * months_ytd, 0),
        }
    finally:
        con.close()


def cost_breakdown() -> list[CostBreakdownItem]:
    """Major cost categories for pie chart — current month spend.

    Covers all service types documented in ACCOUNT_USAGE.METERING_HISTORY.
    Any service type not in the explicit lists is captured in 'Other' via a
    NOT IN query rather than an arithmetic residual — so new Snowflake service
    types appear immediately rather than being silently dropped.
    """
    con = _con()
    try:
        m = f"DATE_TRUNC('month', {_as_of_sql()})"
        ai_warehouses = (
            "'ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS'"
        )
        # All AI service types in metering_history
        ai_svc = (
            "'CORTEX_AI_FUNCTIONS','CORTEX_SEARCH','AI_SERVICES',"
            "'CORTEX_ANALYST','DOCUMENT_AI','SNOWFLAKE_INTELLIGENCE',"
            "'CORTEX_AGENTS','CORTEX_GUARDRAILS'"
        )
        # All serverless compute service types (incl. new doc-listed ones)
        serverless_svc = (
            "'AUTOMATIC_CLUSTERING','AUTO_CLUSTERING',"
            "'SNOWPIPE','PIPE','SNOWPIPE_STREAMING',"
            "'SERVERLESS_TASK','SERVERLESS_ALERTS',"
            "'REPLICATION','SEARCH_OPTIMIZATION','MATERIALIZED_VIEW',"
            "'QUERY_ACCELERATION','SNOWPARK_CONTAINER_SERVICES',"
            "'HYBRID_TABLE_REQUESTS',"
            "'OPENFLOW_COMPUTE_BYOC','OPENFLOW_COMPUTE_SNOWFLAKE',"
            "'POSTGRES_COMPUTE','POSTGRES_COMPUTE_HA',"
            "'WAREHOUSE_METERING','WAREHOUSE_METERING_READER'"
        )
        # Storage-related metering types (on top of storage_usage snapshot)
        storage_svc = (
            "'FAILSAFE_RECOVERY',"
            "'ARCHIVE_STORAGE_RETRIEVAL_FILE_PROCESSING',"
            "'ARCHIVE_STORAGE_WRITE',"
            "'STORAGE_LIFECYCLE_POLICY_EXECUTION'"
        )

        def _s(sql: str) -> float:
            row = con.execute(sql).fetchone()
            return float((row[0] if row else 0) or 0)

        # Managed compute — non-AI virtual warehouses
        managed_compute = _s(
            f"SELECT COALESCE(SUM(credits_used), 0) "
            f"FROM warehouse_metering_history "
            f"WHERE start_time >= {m} "
            f"AND warehouse_name NOT IN ({ai_warehouses})"
        ) * CREDIT_PRICE

        # Serverless compute
        serverless_compute = _s(
            f"SELECT COALESCE(SUM(credits_used), 0) "
            f"FROM metering_history "
            f"WHERE start_time >= {m} "
            f"AND service_type IN ({serverless_svc})"
        ) * CREDIT_PRICE

        # AI & ML — AI warehouses + serverless AI services
        ai_wh_credits = _s(
            f"SELECT COALESCE(SUM(credits_used), 0) "
            f"FROM warehouse_metering_history "
            f"WHERE start_time >= {m} "
            f"AND warehouse_name IN ({ai_warehouses})"
        )
        ai_svc_credits = _s(
            f"SELECT COALESCE(SUM(credits_used), 0) "
            f"FROM metering_history "
            f"WHERE start_time >= {m} "
            f"AND service_type IN ({ai_svc})"
        )
        ai_total = (ai_wh_credits + ai_svc_credits) * CREDIT_PRICE

        # Storage — latest daily snapshot + storage-related metering
        st = con.execute(
            "SELECT storage_bytes, stage_bytes, failsafe_bytes "
            "FROM storage_usage ORDER BY usage_date DESC LIMIT 1"
        ).fetchone()
        tb_to_cost = 23.0 / (1024 ** 4)
        storage = (
            float((st[0] if st else 0) or 0)
            + float((st[1] if st else 0) or 0)
            + float((st[2] if st else 0) or 0)
        ) * tb_to_cost
        storage += _s(
            f"SELECT COALESCE(SUM(credits_used), 0) "
            f"FROM metering_history "
            f"WHERE start_time >= {m} "
            f"AND service_type IN ({storage_svc})"
        ) * CREDIT_PRICE

        # Data Transfer
        data_transfer = _s(
            f"SELECT COALESCE(SUM(credits_used), 0) "
            f"FROM metering_history "
            f"WHERE start_time >= {m} "
            f"AND service_type = 'DATA_TRANSFER'"
        ) * CREDIT_PRICE

        # Other — everything in metering_history NOT in any explicit category.
        # Uses NOT IN so new Snowflake service types appear here automatically.
        all_known = f"{ai_svc},{serverless_svc},{storage_svc},'DATA_TRANSFER'"
        other = _s(
            f"SELECT COALESCE(SUM(credits_used), 0) "
            f"FROM metering_history "
            f"WHERE start_time >= {m} "
            f"AND service_type NOT IN ({all_known})"
        ) * CREDIT_PRICE

        return [
            {"label": "Managed Compute", "cost": managed_compute},
            {"label": "Serverless Compute", "cost": serverless_compute},
            {"label": "AI & ML", "cost": ai_total},
            {"label": "Storage", "cost": storage},
            {"label": "Data Transfer", "cost": data_transfer},
            {"label": "Other", "cost": other},
        ]
    finally:
        con.close()


def ai_cost_breakdown() -> list[CostBreakdownItem]:
    """AI service breakdown for pie chart — current month."""
    con = _con()
    try:
        df = con.execute(
            "SELECT service_type, SUM(credits_used) AS credits "
            "FROM metering_history "
            f"WHERE start_time >= DATE_TRUNC('month', {_as_of_sql()}) "
            "AND service_type IN ('CORTEX_AI_FUNCTIONS','CORTEX_SEARCH',"
            "'AI_SERVICES','CORTEX_ANALYST','DOCUMENT_AI',"
            "'SNOWFLAKE_INTELLIGENCE','CORTEX_AGENTS','CORTEX_GUARDRAILS') "
            "GROUP BY service_type ORDER BY credits DESC"
        ).fetchdf()
        results: list[CostBreakdownItem] = []
        if not df.empty:
            for _, row in df.iterrows():
                label = str(row["service_type"]).replace("_", " ").title()
                results.append({"label": label, "cost": float(row["credits"]) * CREDIT_PRICE})
        # Add AI warehouse compute
        wh = con.execute(
            "SELECT warehouse_name, SUM(credits_used) AS credits "
            "FROM warehouse_metering_history "
            f"WHERE start_time >= DATE_TRUNC('month', {_as_of_sql()}) "
            "AND warehouse_name IN ('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS') "
            "GROUP BY warehouse_name ORDER BY credits DESC"
        ).fetchdf()
        if not wh.empty:
            for _, row in wh.iterrows():
                label = str(row["warehouse_name"]).replace("_", " ").title()
                results.append({"label": label, "cost": float(row["credits"]) * CREDIT_PRICE})
        return results
    finally:
        con.close()


def serverless_cost_breakdown() -> list[CostBreakdownItem]:
    """Serverless services breakdown for pie chart — current month."""
    con = _con()
    try:
        df = con.execute(
            "SELECT service_type, SUM(credits_used) AS credits "
            "FROM metering_history "
            f"WHERE start_time >= DATE_TRUNC('month', {_as_of_sql()}) "
            "AND service_type IN ('AUTOMATIC_CLUSTERING','SNOWPIPE','SERVERLESS_TASK',"
            "'REPLICATION','DATA_TRANSFER','SEARCH_OPTIMIZATION',"
            "'MATERIALIZED_VIEW','QUERY_ACCELERATION','SNOWPARK_CONTAINER_SERVICES') "
            "GROUP BY service_type ORDER BY credits DESC"
        ).fetchdf()
        results: list[CostBreakdownItem] = []
        if not df.empty:
            for _, row in df.iterrows():
                label = str(row["service_type"]).replace("_", " ").title()
                results.append({"label": label, "cost": float(row["credits"]) * CREDIT_PRICE})
        return results
    finally:
        con.close()


def top_tables_storage(limit: int = 25) -> pd.DataFrame:
    """Top N tables by total storage with active, time_travel, and failsafe breakdown."""
    return _query(f"""
        SELECT
            CONCAT_WS('.', table_catalog, table_schema, table_name) AS table_name,
            ROUND(active_bytes / POWER(1024, 3), 2) AS active_gb,
            ROUND(time_travel_bytes / POWER(1024, 3), 2) AS time_travel_gb,
            ROUND(failsafe_bytes / POWER(1024, 3), 2) AS failsafe_gb,
            ROUND(
                (active_bytes + time_travel_bytes + failsafe_bytes) / POWER(1024, 3), 2
            ) AS total_gb
        FROM table_storage_metrics
        WHERE table_name IS NOT NULL
        ORDER BY (active_bytes + time_travel_bytes + failsafe_bytes) DESC
        LIMIT {limit}
    """)


def top_users_hidden_waste(top_n: int = 5) -> pd.DataFrame:
    """Top N users contributing to hidden waste across all service types.

    Returns: User, TCO by User, Attributed Waste, Service Type, Comment
    """
    # Resolve waste via the helpers (Parquet or computed). Never ``FROM hidden_waste_*``
    # in SQL here: live lake dumps omit those demo tables, and DuckDB's Python
    # replacement scan then binds the same-named module functions and raises.
    if not (
        _has_table("query_history") and _has_table("query_attribution_history")
    ):
        return pd.DataFrame()

    compute_df = hidden_waste_compute()
    ai_df = hidden_waste_ai()

    con = _con()
    try:
        # 1. Get each user's total spend (TCO proxy) from query attribution last 30 days
        user_spend = con.execute(f"""
            SELECT q.user_name,
                   SUM(a.credits_attributed_compute) * {CREDIT_PRICE} AS tco_by_user
            FROM query_history q
            JOIN query_attribution_history a ON q.query_id = a.query_id
            WHERE q.start_time >= {_as_of_sql()} - INTERVAL 30 DAY
            GROUP BY q.user_name
            HAVING tco_by_user > 0
        """).fetchdf()

        if user_spend.empty:
            return pd.DataFrame()

        # 2. Compute waste attribution (by warehouse share)
        if compute_df.empty:
            compute_waste = pd.DataFrame()
        else:
            con.register("_hw_compute", compute_df)
            compute_waste = con.execute(f"""
                WITH wh_waste AS (
                    SELECT warehouse_name, SUM(wasted_cost_usd) AS waste
                    FROM _hw_compute
                    GROUP BY warehouse_name
                ),
                user_wh AS (
                    SELECT q.user_name, q.warehouse_name,
                           SUM(a.credits_attributed_compute) AS user_credits
                    FROM query_history q
                    JOIN query_attribution_history a ON q.query_id = a.query_id
                    WHERE q.start_time >= {_as_of_sql()} - INTERVAL 30 DAY
                    GROUP BY q.user_name, q.warehouse_name
                ),
                wh_totals AS (
                    SELECT warehouse_name, SUM(user_credits) AS total_credits
                    FROM user_wh GROUP BY warehouse_name
                )
                SELECT u.user_name,
                       SUM(u.user_credits / NULLIF(t.total_credits, 0) * w.waste)
                           AS attributed_waste,
                       'Compute' AS service_type,
                       FIRST(u.warehouse_name ORDER BY u.user_credits DESC) AS primary_wh
                FROM user_wh u
                JOIN wh_totals t ON u.warehouse_name = t.warehouse_name
                JOIN wh_waste w ON u.warehouse_name = w.warehouse_name
                GROUP BY u.user_name
            """).fetchdf()

        # 3. AI waste attribution (users on AI-type warehouses share total AI waste)
        total_ai_waste = (
            float(ai_df["wasted_cost_usd"].sum())
            if not ai_df.empty and "wasted_cost_usd" in ai_df.columns
            else 0.0
        )

        if total_ai_waste and total_ai_waste > 0:
            ai_warehouses = (
                "('CORTEX_AI', 'CORTEX_SEARCH', 'CORTEX_AGENTS', "
                "'ML_TRAINING', 'DATA_SCIENCE')"
            )
            ai_waste = con.execute(f"""
                WITH user_ai_credits AS (
                    SELECT q.user_name,
                           SUM(a.credits_attributed_compute) AS user_credits
                    FROM query_history q
                    JOIN query_attribution_history a ON q.query_id = a.query_id
                    WHERE q.start_time >= {_as_of_sql()} - INTERVAL 30 DAY
                      AND q.warehouse_name IN {ai_warehouses}
                    GROUP BY q.user_name
                ),
                total AS (
                    SELECT SUM(user_credits) AS grand_total FROM user_ai_credits
                )
                SELECT u.user_name,
                       u.user_credits / NULLIF(t.grand_total, 0)
                           * {total_ai_waste} AS attributed_waste,
                       'AI & ML' AS service_type,
                       'AI warehouses' AS primary_wh
                FROM user_ai_credits u, total t
            """).fetchdf()
        else:
            ai_waste = pd.DataFrame()

        # 4. Combine all waste sources
        all_waste = pd.concat([compute_waste, ai_waste], ignore_index=True)

        if all_waste.empty:
            return pd.DataFrame()

        # Pick the largest waste contribution per user (across service types)
        all_waste = all_waste.sort_values("attributed_waste", ascending=False)
        all_waste = all_waste.drop_duplicates(subset=["user_name"], keep="first")

        # Merge with user spend
        result = all_waste.merge(user_spend, on="user_name", how="left")
        result = result.sort_values("attributed_waste", ascending=False).head(top_n)

        # Build comment based on service type and primary warehouse
        def _comment(row: pd.Series) -> str:
            if row["service_type"] == "AI & ML":
                return f"Oversized model usage on {row['primary_wh']}"
            return f"High idle/spill on {row['primary_wh']}"

        result["comment"] = result.apply(_comment, axis=1)
        result["attributed_waste"] = result["attributed_waste"].round(0)
        result["tco_by_user"] = result["tco_by_user"].round(0)
        result = result[["user_name", "tco_by_user", "attributed_waste", "service_type", "comment"]]
        return result
    finally:
        con.close()


def tco_monthly_trend_and_forecast() -> pd.DataFrame:
    """Monthly TCO — complete months as Actual, current partial month + 5 ahead as Forecast.

    Only full months (1st to last day) count as Actual. The current month is treated
    as Forecast (projected from partial data to full-month estimate).
    """
    con = _con()
    try:
        import numpy as np

        # Monthly credits for complete months only (up to 6 months back)
        df = con.execute(f"""
            WITH wh AS (
                SELECT DATE_TRUNC('month', start_time) AS month,
                       SUM(credits_used) AS credits
                FROM warehouse_metering_history
                WHERE start_time >= DATE_TRUNC('month', {_as_of_sql()} - INTERVAL 6 MONTH)
                  AND start_time < DATE_TRUNC('month', {_as_of_sql()})
                GROUP BY DATE_TRUNC('month', start_time)
            ),
            svc AS (
                SELECT DATE_TRUNC('month', start_time) AS month,
                       SUM(credits_used) AS credits
                FROM metering_history
                WHERE start_time >= DATE_TRUNC('month', {_as_of_sql()} - INTERVAL 6 MONTH)
                  AND start_time < DATE_TRUNC('month', {_as_of_sql()})
                GROUP BY DATE_TRUNC('month', start_time)
            )
            SELECT COALESCE(w.month, s.month) AS month,
                   COALESCE(w.credits, 0) + COALESCE(s.credits, 0) AS total_credits
            FROM wh w
            FULL OUTER JOIN svc s ON w.month = s.month
            ORDER BY month
        """).fetchdf()
        if df.empty:
            return pd.DataFrame()

        # Storage cost (monthly, latest)
        st = con.execute(
            "SELECT ROUND((storage_bytes + stage_bytes + failsafe_bytes) "
            "/ POWER(1024, 4), 2) FROM storage_usage "
            "ORDER BY usage_date DESC LIMIT 1"
        ).fetchone()
        storage_tb = _as_float(st[0] if st else None)
        storage_monthly = storage_tb * 23

        df = _coerce_decimal_columns(df)
        df["total_credits"] = pd.to_numeric(df["total_credits"], errors="coerce").fillna(0.0)
        df["tco"] = df["total_credits"] * CREDIT_PRICE + storage_monthly
        df["month"] = _as_naive_timestamp(df["month"])
        # Normalize to 30-day equivalent so short months (Feb=28d) don't dip
        days_in_month = df["month"].dt.days_in_month
        df["tco"] = df["tco"] * (30.0 / days_in_month)
        df["type"] = "Actual"

        # Linear regression on complete months for forecast
        x = np.arange(len(df)).astype(float)
        y = df["tco"].values
        slope, intercept = np.polyfit(x, y, 1)

        # Forecast: current month (projected from partial) + next 5 months
        # (6 projected bars total — avoids extending into a 7th month like 2027-02).
        forecast_rows = []
        for i in range(6):
            future_month = df["month"].max() + pd.DateOffset(months=i + 1)
            projected = intercept + slope * (len(df) - 1 + i + 1)
            forecast_rows.append({
                "month": future_month,
                "total_credits": 0,
                "tco": max(projected, 0),
                "type": "Forecast",
            })

        result = pd.concat([df, pd.DataFrame(forecast_rows)], ignore_index=True)
        return result
    finally:
        con.close()


def tco_by_month() -> dict[date, float]:
    """Complete-month ACCOUNT_USAGE TCO keyed by month-start date.

    Same numbers the Visibility Trend tab charts as Actual — not the as-of
    ``kpi_summary`` window (which can land on a partial current month).
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


def top_users_daily_credits(top_n: int = 10, user_type: str = "all") -> pd.DataFrame:
    """Top N users by credited spend, pivoted by day for heatmap.

    user_type: 'all', 'service', or 'adhoc'
    """
    cols = ["user_name", "usage_date", "daily_credits"]
    if not (
        _has_table("query_history") and _has_table("query_attribution_history")
    ):
        return pd.DataFrame(columns=cols)
    if user_type == "service":
        user_filter = ("AND (q.user_name LIKE '%_SVC' OR q.user_name IN "
                       "('ETL_SERVICE','ML_PIPELINE','DBT_RUNNER'))")
    elif user_type == "adhoc":
        user_filter = ("AND q.user_name NOT LIKE '%_SVC' AND q.user_name NOT IN "
                       "('ETL_SERVICE','ML_PIPELINE','DBT_RUNNER')")
    else:
        user_filter = ""

    return _query(f"""
        WITH user_credits AS (
            SELECT
                q.user_name,
                CAST(q.start_time AS DATE) AS usage_date,
                SUM(a.credits_attributed_compute) AS daily_credits
            FROM query_history q
            JOIN query_attribution_history a ON q.query_id = a.query_id
            WHERE q.start_time >= {_as_of_sql()} - INTERVAL 21 DAY
              AND a.credits_attributed_compute > 0
              {user_filter}
            GROUP BY q.user_name, CAST(q.start_time AS DATE)
        ),
        top_users AS (
            SELECT user_name, SUM(daily_credits) AS total
            FROM user_credits
            GROUP BY user_name
            ORDER BY total DESC
            LIMIT {top_n}
        )
        SELECT uc.user_name, uc.usage_date, uc.daily_credits
        FROM user_credits uc
        JOIN top_users tu ON uc.user_name = tu.user_name
        ORDER BY tu.total DESC, uc.usage_date
    """)


def warehouse_daily_credits(top_n: int = 10) -> pd.DataFrame:
    """Top N warehouses by credit spend, pivoted by day for heatmap."""
    return _query(f"""
        WITH wh_credits AS (
            SELECT
                warehouse_name,
                CAST(start_time AS DATE) AS usage_date,
                SUM(credits_used) AS daily_credits
            FROM warehouse_metering_history
            WHERE start_time >= {_as_of_sql()} - INTERVAL 21 DAY
              AND warehouse_id > 0
            GROUP BY warehouse_name, CAST(start_time AS DATE)
        ),
        top_wh AS (
            SELECT warehouse_name, SUM(daily_credits) AS total
            FROM wh_credits
            GROUP BY warehouse_name
            ORDER BY total DESC
            LIMIT {top_n}
        )
        SELECT wc.warehouse_name, wc.usage_date, wc.daily_credits
        FROM wh_credits wc
        JOIN top_wh tw ON wc.warehouse_name = tw.warehouse_name
        ORDER BY tw.total DESC, wc.usage_date
    """)


def warehouse_spend_filtered(
    start: str, end: str, rollup: str = "day",
) -> pd.DataFrame:
    """Warehouse spend aggregated by rollup grain within date range."""
    trunc = {"day": "day", "week": "week", "month": "month",
             "year": "year"}[rollup]
    return _query(f"""
        SELECT DATE_TRUNC('{trunc}', start_time) AS period,
               warehouse_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM warehouse_metering_history
        WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
              AND warehouse_id > 0
        GROUP BY period, warehouse_name
        ORDER BY period, SUM(credits_used) DESC
    """)


def warehouse_summary_filtered(start: str, end: str) -> pd.DataFrame:
    """Warehouse total spend within date range."""
    return _query(f"""
        SELECT warehouse_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM warehouse_metering_history
        WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
              AND warehouse_id > 0
        GROUP BY warehouse_name
        ORDER BY SUM(credits_used) DESC
    """)


def idle_warehouses_filtered(start: str, end: str) -> pd.DataFrame:
    """P01: Idle warehouses within date range."""
    return _query(f"""
        WITH metering AS (
            SELECT warehouse_name, SUM(credits_used) AS credits_used
            FROM warehouse_metering_history
            WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
                  AND warehouse_id > 0
            GROUP BY warehouse_name
        ),
        load AS (
            SELECT warehouse_name,
                   AVG(avg_running) AS avg_running,
                   AVG(avg_queued_load) AS avg_queued_load
            FROM warehouse_load_history
            WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
            GROUP BY warehouse_name
        )
        SELECT m.warehouse_name AS object_name,
               ROUND(m.credits_used, 2) AS credits,
               ROUND(m.credits_used * {CREDIT_PRICE}, 2) AS cost_usd,
               ROUND(COALESCE(l.avg_running, 0), 3) AS avg_running,
               ROUND(COALESCE(l.avg_queued_load, 0), 3) AS avg_queued_load,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.15
                         AND m.credits_used >= 100 THEN 'BLOCKER'
                    WHEN COALESCE(l.avg_running, 0) < 0.30
                         AND m.credits_used >= 25 THEN 'WARN'
                    ELSE 'INFO' END AS severity
        FROM metering m LEFT JOIN load l USING (warehouse_name)
        WHERE m.credits_used >= 10
        ORDER BY m.credits_used DESC
    """)


# ── Hidden Waste ───────────────────────────────────────────────────────────────

_STORAGE_COST_PER_TB = 23.0


def hidden_waste_compute() -> pd.DataFrame:
    """Compute hidden waste: idle running, oversized warehouses."""
    if _has_table("hidden_waste_compute"):
        # Qualify ``main.`` so DuckDB does not bind this module's function via
        # Python replacement scans (same name as the view).
        return _query("""
            SELECT warehouse_name, waste_type, idle_hours,
                   actual_cost_usd, wasted_credits, wasted_cost_usd, recommendation, size
            FROM main.hidden_waste_compute
            ORDER BY wasted_cost_usd DESC
        """)
    if not (
        _has_table("warehouse_metering_history") and _has_table("warehouse_load_history")
    ):
        return pd.DataFrame(
            columns=[
                "warehouse_name",
                "waste_type",
                "idle_hours",
                "actual_cost_usd",
                "wasted_credits",
                "wasted_cost_usd",
                "recommendation",
                "size",
            ]
        )
    # Live ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY has no warehouse_size (demo-only).
    # Prefer size from query_history when present; otherwise label UNKNOWN.
    as_of = _as_of_sql()
    size_join = ""
    if _has_column("warehouse_metering_history", "warehouse_size"):
        size_select = "warehouse_size,"
        size_group = ", warehouse_size"
        size_out = "COALESCE(m.warehouse_size, 'UNKNOWN')"
    elif _has_column("query_history", "warehouse_size"):
        size_select = ""
        size_group = ""
        size_out = "COALESCE(s.warehouse_size, 'UNKNOWN')"
        size_join = f"""
        LEFT JOIN (
            SELECT warehouse_name,
                   arg_max(warehouse_size, start_time) AS warehouse_size
            FROM query_history
            WHERE warehouse_size IS NOT NULL
              AND start_time >= {as_of} - INTERVAL 30 DAY
            GROUP BY warehouse_name
        ) s USING (warehouse_name)
        """
    else:
        size_select = ""
        size_group = ""
        size_out = "'UNKNOWN'"

    return _query(f"""
        WITH metering AS (
            SELECT warehouse_name,
                   {size_select}
                   SUM(credits_used) AS credits_used
            FROM warehouse_metering_history
            WHERE start_time >= {as_of} - INTERVAL 30 DAY
            GROUP BY warehouse_name{size_group}
        ),
        load AS (
            SELECT warehouse_name,
                   AVG(avg_running) AS avg_running
            FROM warehouse_load_history
            WHERE start_time >= {as_of} - INTERVAL 30 DAY
            GROUP BY warehouse_name
        )
        SELECT m.warehouse_name AS warehouse_name,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.10 THEN 'idle_running'
                    ELSE 'oversized' END AS waste_type,
               ROUND(m.credits_used * (1 - COALESCE(l.avg_running, 0)), 1) AS idle_hours,
               ROUND(m.credits_used * COALESCE(1 - l.avg_running, 0.5), 2)
                   AS wasted_credits,
               ROUND(m.credits_used * COALESCE(1 - l.avg_running, 0.5)
                     * {CREDIT_PRICE}, 2) AS wasted_cost_usd,
               ROUND(m.credits_used * {CREDIT_PRICE}, 2) AS actual_cost_usd,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.10
                    THEN 'Enable auto-suspend (60s); consider X-Small for dev'
                    ELSE 'Scale down warehouse size; review concurrent usage'
               END AS recommendation,
               {size_out} AS size
        FROM metering m
        LEFT JOIN load l USING (warehouse_name)
        {size_join}
        WHERE m.credits_used >= 10
          AND COALESCE(l.avg_running, 0) < 0.50
        ORDER BY wasted_cost_usd DESC
    """)


def hidden_waste_storage() -> pd.DataFrame:
    """Storage hidden waste: stale tables, TT excess, abandoned clones."""
    if _has_table("hidden_waste_storage"):
        return _query("""
            SELECT object_name, waste_type, size_gb,
                   days_since_access, actual_cost_usd, monthly_cost_usd, recommendation
            FROM main.hidden_waste_storage
            ORDER BY monthly_cost_usd DESC
        """)
    if not _has_table("table_storage_metrics"):
        return pd.DataFrame(
            columns=[
                "object_name",
                "waste_type",
                "size_gb",
                "days_since_access",
                "actual_cost_usd",
                "monthly_cost_usd",
                "recommendation",
            ]
        )
    return _query(f"""
        SELECT CONCAT_WS('.', table_catalog, table_schema, table_name)
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
        FROM table_storage_metrics
        WHERE table_name IS NOT NULL
          AND (time_travel_bytes + failsafe_bytes) > POWER(1024, 3)
        ORDER BY (time_travel_bytes + failsafe_bytes) DESC
        LIMIT 50
    """)


def hidden_waste_ai() -> pd.DataFrame:
    """AI/Cortex hidden waste: 6 patterns from snowflake-ai-finops."""
    if _has_table("hidden_waste_ai"):
        return _query("""
            SELECT waste_pattern, function_name, model_name, task_type,
                   calls_30d, actual_cost_usd, wasted_credits, wasted_cost_usd, recommendation
            FROM main.hidden_waste_ai
            ORDER BY wasted_cost_usd DESC
        """)
    return pd.DataFrame(
        columns=[
            "waste_pattern",
            "function_name",
            "model_name",
            "task_type",
            "calls_30d",
            "actual_cost_usd",
            "wasted_credits",
            "wasted_cost_usd",
            "recommendation",
        ]
    )


def hidden_waste_summary() -> dict[str, Any]:
    """Total hidden waste across all pillars (based on last 30 days)."""
    try:
        compute_df = hidden_waste_compute()
        compute = float(compute_df["wasted_cost_usd"].sum()) if not compute_df.empty else 0.0
        storage_df = hidden_waste_storage()
        storage = (
            float(storage_df["monthly_cost_usd"].sum() * 12) if not storage_df.empty else 0.0
        )
        ai_df = hidden_waste_ai()
        ai_waste = float(ai_df["wasted_cost_usd"].sum()) if not ai_df.empty else 0.0
        as_of = _as_of_sql()
        con = _con()
        try:
            wh_30d = con.execute(
                "SELECT COALESCE(SUM(credits_used), 0) FROM warehouse_metering_history "
                f"WHERE start_time >= {as_of} - INTERVAL 30 DAY"
            ).fetchone()
            svc_30d = con.execute(
                "SELECT COALESCE(SUM(credits_used), 0) FROM metering_history "
                f"WHERE start_time >= {as_of} - INTERVAL 30 DAY"
            ).fetchone()
        finally:
            con.close()
        spend_30d = (
            ((wh_30d[0] if wh_30d else 0) or 0) + ((svc_30d[0] if svc_30d else 0) or 0)
        ) * CREDIT_PRICE
        total_waste = round(compute + storage + ai_waste, 0)
        return {
            "compute": round(compute, 0),
            "storage_annual": round(storage, 0),
            "ai": round(ai_waste, 0),
            "total": total_waste,
            "spend_30d": round(spend_30d, 0),
            "waste_pct": round(total_waste / max(spend_30d, 1) * 100, 1),
        }
    except Exception:  # noqa: BLE001
        return {
            "compute": 0,
            "storage_annual": 0,
            "ai": 0,
            "total": 0,
            "spend_30d": 0,
            "waste_pct": 0.0,
        }



def queue_pressure_filtered(start: str, end: str) -> pd.DataFrame:
    """P03: Queue pressure within date range."""
    return _query(f"""
        SELECT warehouse_name AS object_name,
               ROUND(SUM(queued_overload_time) / 1000.0, 1)
               AS queued_seconds,
               COUNT(*) AS queries
        FROM query_history
        WHERE CAST(start_time AS DATE) BETWEEN '{start}' AND '{end}'
              AND warehouse_name IS NOT NULL
        GROUP BY warehouse_name
        HAVING SUM(queued_overload_time) > 0
        ORDER BY SUM(queued_overload_time) DESC
    """)


# ── Compute / Platform (unfiltered) ───────────────────────────────────────────

def idle_warehouses() -> pd.DataFrame:
    """P01: Idle or underused warehouses."""
    return _query(f"""
        WITH metering AS (
            SELECT warehouse_name, SUM(credits_used) AS credits_used
            FROM warehouse_metering_history
            WHERE warehouse_id > 0
            GROUP BY warehouse_name
        ),
        load AS (
            SELECT warehouse_name,
                   AVG(avg_running) AS avg_running,
                   AVG(avg_queued_load) AS avg_queued_load
            FROM warehouse_load_history
            GROUP BY warehouse_name
        )
        SELECT m.warehouse_name AS object_name,
               ROUND(m.credits_used, 2) AS credits,
               ROUND(m.credits_used * {CREDIT_PRICE}, 2) AS cost_usd,
               ROUND(COALESCE(l.avg_running, 0), 3) AS avg_running,
               ROUND(COALESCE(l.avg_queued_load, 0), 3) AS avg_queued_load,
               CASE WHEN COALESCE(l.avg_running, 0) < 0.15 AND m.credits_used >= 100 THEN 'BLOCKER'
                    WHEN COALESCE(l.avg_running, 0) < 0.30 AND m.credits_used >= 25 THEN 'WARN'
                    ELSE 'INFO' END AS severity
        FROM metering m LEFT JOIN load l USING (warehouse_name)
        WHERE m.credits_used >= 10
        ORDER BY m.credits_used DESC
    """)


def warehouse_cost_efficiency() -> pd.DataFrame:
    """P08: Warehouse credits per scanned TB and per 1000 queries."""
    cols = [
        "object_name",
        "credits",
        "cost_usd",
        "queries",
        "scanned_tb",
        "credits_per_tb",
        "credits_per_1k_queries",
        "avg_cache_pct",
        "spilled_gb",
    ]
    if not _has_table("query_history"):
        return pd.DataFrame(columns=cols)
    # Attribution is optional at ingest — without it there is no credit numerator.
    if not _has_table("query_attribution_history"):
        return pd.DataFrame(columns=cols)
    return _query(f"""
        WITH wqc AS (
            SELECT q.warehouse_name,
                   COUNT(DISTINCT q.query_id) AS queries,
                   SUM(COALESCE(a.credits_attributed_compute, 0)) AS credits,
                   SUM(COALESCE(q.bytes_scanned, 0)) AS bytes_scanned,
                   SUM(COALESCE(q.bytes_spilled_to_local_storage, 0) +
                       COALESCE(q.bytes_spilled_to_remote_storage, 0)) AS bytes_spilled,
                   AVG(COALESCE(q.percentage_scanned_from_cache, 0)) AS avg_cache_pct
            FROM query_history q
            LEFT JOIN query_attribution_history a ON q.query_id = a.query_id
            WHERE q.execution_status = 'SUCCESS' AND q.warehouse_name IS NOT NULL
            GROUP BY q.warehouse_name
        )
        SELECT warehouse_name AS object_name,
               ROUND(credits, 2) AS credits,
               ROUND(credits * {CREDIT_PRICE}, 2) AS cost_usd,
               queries,
               ROUND(bytes_scanned / POWER(1024, 4), 2) AS scanned_tb,
               ROUND(credits / NULLIF(bytes_scanned / POWER(1024, 4), 0), 2) AS credits_per_tb,
               ROUND(1000 * credits / NULLIF(queries, 0), 2) AS credits_per_1k_queries,
               ROUND(avg_cache_pct, 1) AS avg_cache_pct,
               ROUND(bytes_spilled / POWER(1024, 3), 2) AS spilled_gb
        FROM wqc WHERE credits > 0
        ORDER BY credits DESC
    """)


def queue_pressure() -> pd.DataFrame:
    """P03: Warehouse queue pressure."""
    return _query("""
        SELECT warehouse_name AS object_name,
               ROUND(SUM(queued_overload_time) / 1000.0, 1) AS queued_seconds,
               COUNT(*) AS queries
        FROM query_history
        WHERE warehouse_name IS NOT NULL
        GROUP BY warehouse_name
        HAVING SUM(queued_overload_time) > 0
        ORDER BY SUM(queued_overload_time) DESC
    """)


# ── Queries ────────────────────────────────────────────────────────────────────

def query_attributed_cost() -> pd.DataFrame:
    """Q00: Top query patterns by attributed compute credits."""
    cols = [
        "object_name",
        "queries",
        "credits",
        "cost_usd",
        "qas_credits",
        "warehouse",
    ]
    if not _has_table("query_attribution_history"):
        return pd.DataFrame(columns=cols)
    return _query(f"""
        SELECT query_parameterized_hash AS object_name,
               COUNT(*) AS queries,
               ROUND(SUM(credits_attributed_compute), 2) AS credits,
               ROUND(SUM(credits_attributed_compute) * {CREDIT_PRICE}, 2) AS cost_usd,
               ROUND(SUM(COALESCE(credits_used_query_acceleration, 0)), 2) AS qas_credits,
               ANY_VALUE(warehouse_name) AS warehouse
        FROM query_attribution_history
        WHERE query_parameterized_hash IS NOT NULL
        GROUP BY query_parameterized_hash
        HAVING SUM(credits_attributed_compute) >= 1
        ORDER BY SUM(credits_attributed_compute) DESC
        LIMIT 50
    """)


def expensive_query_patterns() -> pd.DataFrame:
    """Q01: Expensive queries by scan, spill, elapsed."""
    return _query("""
        SELECT query_hash AS object_name,
               COUNT(*) AS queries,
               ROUND(SUM(bytes_scanned) / POWER(1024, 4), 2) AS tb_scanned,
               ROUND(SUM(bytes_spilled_to_remote_storage) / POWER(1024, 3), 2) AS gb_remote_spill,
               ROUND(MAX(total_elapsed_time) / 1000.0, 1) AS max_elapsed_sec,
               ANY_VALUE(warehouse_name) AS warehouse
        FROM query_history
        WHERE execution_status = 'SUCCESS' AND query_hash IS NOT NULL
        GROUP BY query_hash
        HAVING COUNT(*) >= 3
            AND (SUM(bytes_scanned) > POWER(1024, 4)
                 OR SUM(bytes_spilled_to_remote_storage) > 0
                 OR MAX(total_elapsed_time) > 300000)
        ORDER BY SUM(bytes_scanned) DESC
        LIMIT 50
    """)


def cache_reuse_opportunity() -> pd.DataFrame:
    """Q09: Recurring patterns with poor cache reuse."""
    return _query("""
        SELECT COALESCE(query_parameterized_hash, query_hash) AS object_name,
               COUNT(*) AS queries,
               ROUND(SUM(bytes_scanned) / POWER(1024, 4), 2) AS tb_scanned,
               ROUND(AVG(COALESCE(percentage_scanned_from_cache, 0)), 1) AS avg_cache_pct,
               COUNT(DISTINCT warehouse_name) AS warehouses
        FROM query_history
        WHERE execution_status = 'SUCCESS' AND query_type = 'SELECT'
              AND COALESCE(query_parameterized_hash, query_hash) IS NOT NULL
        GROUP BY COALESCE(query_parameterized_hash, query_hash)
        HAVING COUNT(*) >= 5 AND AVG(COALESCE(percentage_scanned_from_cache, 0)) < 40
        ORDER BY SUM(bytes_scanned) DESC
        LIMIT 50
    """)


# ── Storage ────────────────────────────────────────────────────────────────────

def storage_trend() -> pd.DataFrame:
    """S01: Account storage trend."""
    return _query("""
        SELECT usage_date AS date,
               ROUND(storage_bytes / POWER(1024, 4), 3) AS table_tb,
               ROUND(stage_bytes / POWER(1024, 4), 3) AS stage_tb,
               ROUND(failsafe_bytes / POWER(1024, 4), 3) AS failsafe_tb,
               ROUND(COALESCE(hybrid_table_storage_bytes, 0) / POWER(1024, 4), 3) AS hybrid_tb,
               ROUND(COALESCE(archive_storage_cool_bytes, 0)
                     / POWER(1024, 4), 3) AS archive_cool_tb,
               ROUND(COALESCE(archive_storage_cold_bytes, 0)
                     / POWER(1024, 4), 3) AS archive_cold_tb
        FROM storage_usage
        ORDER BY usage_date
    """)


def top_tables() -> pd.DataFrame:
    """S02: Largest tables."""
    return _query("""
        SELECT CONCAT_WS('.', table_catalog, table_schema, table_name) AS object_name,
               ROUND(active_bytes / POWER(1024, 3), 2) AS active_gb,
               ROUND(time_travel_bytes / POWER(1024, 3), 2) AS time_travel_gb,
               ROUND(failsafe_bytes / POWER(1024, 3), 2) AS failsafe_gb,
               ROUND(retained_for_clone_bytes / POWER(1024, 3), 2) AS clone_gb,
               is_transient AS transient
        FROM table_storage_metrics
        WHERE table_name IS NOT NULL
          AND (active_bytes + time_travel_bytes + failsafe_bytes) > 0
        ORDER BY (active_bytes + time_travel_bytes + failsafe_bytes
                  + retained_for_clone_bytes) DESC
        LIMIT 30
    """)


# ── Governance ─────────────────────────────────────────────────────────────────

def unattributed_spend() -> pd.DataFrame:
    """G01: Warehouses missing owner/cost-center tags."""
    cols = ["object_name", "credits", "cost_usd", "owner", "cost_center"]
    if not _has_table("warehouse_metering_history"):
        return pd.DataFrame(columns=cols)
    # TAG_REFERENCES is optional at ingest; without it every warehouse is untagged.
    if not _has_table("tag_references"):
        return _query(f"""
            SELECT warehouse_name AS object_name,
                   ROUND(SUM(credits_used), 2) AS credits,
                   ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd,
                   '<missing>' AS owner,
                   '<missing>' AS cost_center
            FROM warehouse_metering_history
            WHERE warehouse_id > 0
            GROUP BY warehouse_name
            ORDER BY SUM(credits_used) DESC
        """)
    return _query(f"""
        WITH wh_spend AS (
            SELECT warehouse_name, SUM(credits_used) AS credits_used
            FROM warehouse_metering_history WHERE warehouse_id > 0
            GROUP BY warehouse_name
        ),
        tag_refs AS (
            SELECT object_name AS warehouse_name,
                   MAX(CASE WHEN tag_name IN ('owner', 'business_owner', 'team')
                       THEN tag_value END) AS owner_tag,
                   MAX(CASE WHEN tag_name IN ('cost_center', 'costcenter')
                       THEN tag_value END) AS cost_center_tag
            FROM tag_references WHERE domain = 'WAREHOUSE'
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


def spend_by_warehouse() -> pd.DataFrame:
    """Monthly spend breakdown by warehouse (for pie/bar charts)."""
    return _query(f"""
        SELECT warehouse_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM warehouse_metering_history
        WHERE warehouse_id > 0
        GROUP BY warehouse_name
        ORDER BY SUM(credits_used) DESC
    """)


def spend_by_day(start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """Daily spend trend, optionally filtered by date range."""
    where = "WHERE warehouse_id > 0"
    if start:
        where += f" AND CAST(start_time AS DATE) >= '{start}'"
    if end:
        where += f" AND CAST(start_time AS DATE) <= '{end}'"
    return _query(f"""
        SELECT CAST(start_time AS DATE) AS date,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM warehouse_metering_history
        {where}
        GROUP BY CAST(start_time AS DATE)
        ORDER BY date
    """)


# ── AI / Cortex ───────────────────────────────────────────────────────────────

def ai_spend_summary() -> dict[str, Any]:
    """AI spend KPIs — combines AI warehouse spend + serverless AI metering (current month)."""
    con = _con()
    try:
        # AI warehouse credits (ML_TRAINING, CORTEX_AI, CORTEX_SEARCH, CORTEX_AGENTS)
        wh_row = con.execute(
            "SELECT ROUND(SUM(credits_used), 0) FROM warehouse_metering_history "
            "WHERE warehouse_name IN "
            "('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS') "
            f"AND start_time >= DATE_TRUNC('month', {_as_of_sql()})"
        ).fetchone()
        wh_credits = wh_row[0] if wh_row else 0
        # Serverless AI metering (exclude managed services like clustering, pipes, etc.)
        svc_row = con.execute(
            "SELECT ROUND(SUM(credits_used), 0) FROM metering_history "
            f"WHERE start_time >= DATE_TRUNC('month', {_as_of_sql()}) "
            "AND service_type IN ('CORTEX_AI_FUNCTIONS','CORTEX_SEARCH',"
            "'AI_SERVICES','CORTEX_ANALYST','DOCUMENT_AI',"
            "'SNOWFLAKE_INTELLIGENCE','CORTEX_AGENTS','CORTEX_GUARDRAILS')"
        ).fetchone()
        svc_credits = svc_row[0] if svc_row else 0
        total_ai = (wh_credits or 0) + (svc_credits or 0)
        return {
            "ai_credits": total_ai,
            "ai_cost": round(total_ai * CREDIT_PRICE, 0),
        }
    finally:
        con.close()


def ai_service_metering() -> pd.DataFrame:
    """AI01: Cortex/AI service credits breakdown."""
    return _query(f"""
        SELECT service_type || ':' || name AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd,
               ANY_VALUE(entity_type) AS entity_type
        FROM metering_history
        GROUP BY service_type, name
        HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
    """)


def ai_function_usage() -> pd.DataFrame:
    """AI02: Cortex AI function usage by function and model.

    Synthetic demo Parquet has flat ``calls`` / ``tokens_sent`` / ``tokens_received``.
    Live ACCOUNT_USAGE.CORTEX_AI_FUNCTIONS_USAGE_HISTORY stores tokens in a
    ``metrics`` VARIANT (JSON) and has one row per call — derive those columns.
    """
    cols = [
        "object_name",
        "credits",
        "cost_usd",
        "calls",
        "tokens_sent",
        "tokens_received",
        "users",
    ]
    if not _has_table("cortex_ai_functions_usage_history"):
        return pd.DataFrame(columns=cols)
    if _has_column("cortex_ai_functions_usage_history", "calls"):
        return _query(f"""
            SELECT function_name || ':' || model_name AS object_name,
                   ROUND(SUM(credits), 2) AS credits,
                   ROUND(SUM(credits) * {CREDIT_PRICE}, 2) AS cost_usd,
                   SUM(calls) AS calls,
                   SUM(tokens_sent) AS tokens_sent,
                   SUM(tokens_received) AS tokens_received,
                   COUNT(DISTINCT user_id) AS users
            FROM cortex_ai_functions_usage_history
            GROUP BY function_name, model_name
            HAVING SUM(credits) > 0
            ORDER BY SUM(credits) DESC
        """)
    # Live schema: metrics JSON list of {{key.metric, value}} token readings.
    return _query(f"""
        WITH base AS (
            SELECT function_name, model_name, credits, user_id,
                   (
                       SELECT SUM(CAST(json_extract(x, '$.value') AS BIGINT))
                       FROM UNNEST(CAST(metrics AS JSON)::JSON[]) AS t(x)
                       WHERE json_extract_string(x, '$.key.metric') = 'input'
                   ) AS tokens_sent,
                   (
                       SELECT SUM(CAST(json_extract(x, '$.value') AS BIGINT))
                       FROM UNNEST(CAST(metrics AS JSON)::JSON[]) AS t(x)
                       WHERE json_extract_string(x, '$.key.metric') = 'output'
                   ) AS tokens_received
            FROM cortex_ai_functions_usage_history
        )
        SELECT function_name || ':' || model_name AS object_name,
               ROUND(SUM(credits), 2) AS credits,
               ROUND(SUM(credits) * {CREDIT_PRICE}, 2) AS cost_usd,
               COUNT(*) AS calls,
               SUM(COALESCE(tokens_sent, 0)) AS tokens_sent,
               SUM(COALESCE(tokens_received, 0)) AS tokens_received,
               COUNT(DISTINCT user_id) AS users
        FROM base
        GROUP BY function_name, model_name
        HAVING SUM(credits) > 0
        ORDER BY SUM(credits) DESC
    """)


def ai_search_daily() -> pd.DataFrame:
    """AI03: Cortex Search daily cost by service."""
    cols = ["object_name", "consumption_type", "credits", "cost_usd", "tokens"]
    if not _has_table("cortex_search_daily_usage_history"):
        return pd.DataFrame(columns=cols)
    return _query(f"""
        SELECT database_name || '.' || schema_name || '.' || service_name
               AS object_name,
               consumption_type,
               ROUND(SUM(credits), 2) AS credits,
               ROUND(SUM(credits) * {CREDIT_PRICE}, 2) AS cost_usd,
               SUM(tokens) AS tokens
        FROM cortex_search_daily_usage_history
        GROUP BY database_name, schema_name, service_name, consumption_type
        HAVING SUM(credits) > 0
        ORDER BY SUM(credits) DESC
    """)


def ai_spend_by_day() -> pd.DataFrame:
    """Daily AI spend trend."""
    return _query(f"""
        SELECT CAST(start_time AS DATE) AS date,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM metering_history
        GROUP BY CAST(start_time AS DATE)
        ORDER BY date
    """)


# ── Serverless Services ────────────────────────────────────────────────────────

def serverless_optimization_spend() -> pd.DataFrame:
    """D01: Serverless optimization services."""
    cols = ["object_name", "service_type", "credits", "cost_usd"]
    if not _has_table("automatic_clustering_history"):
        return pd.DataFrame(columns=cols)
    return _query(f"""
        SELECT database_name || '.' || schema_name || '.' || table_name
               AS object_name,
               'AUTOMATIC_CLUSTERING' AS service_type,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM automatic_clustering_history
        GROUP BY database_name, schema_name, table_name
        HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
    """)


def snowpipe_cost() -> pd.DataFrame:
    """I01: Snowpipe credit and file efficiency."""
    cols = ["object_name", "credits", "cost_usd", "gb_inserted", "files"]
    if not _has_table("pipe_usage_history"):
        return pd.DataFrame(columns=cols)
    # Live dumps sometimes store credits/files as VARCHAR — cast before SUM.
    return _query(f"""
        SELECT pipe_name AS object_name,
               ROUND(SUM(TRY_CAST(credits_used AS DOUBLE)), 2) AS credits,
               ROUND(SUM(TRY_CAST(credits_used AS DOUBLE)) * {CREDIT_PRICE}, 2)
                   AS cost_usd,
               ROUND(SUM(TRY_CAST(bytes_inserted AS DOUBLE)) / POWER(1024, 3), 2)
                   AS gb_inserted,
               SUM(TRY_CAST(files_inserted AS BIGINT)) AS files
        FROM pipe_usage_history
        GROUP BY pipe_name
        HAVING SUM(TRY_CAST(credits_used AS DOUBLE)) > 0
        ORDER BY SUM(TRY_CAST(credits_used AS DOUBLE)) DESC
    """)


def serverless_task_costs() -> pd.DataFrame:
    """I03: Serverless task cost profile."""
    cols = ["object_name", "credits", "cost_usd"]
    if not _has_table("serverless_task_history"):
        return pd.DataFrame(columns=cols)
    return _query(f"""
        SELECT database_name || '.' || schema_name || '.' || task_name
               AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM serverless_task_history
        GROUP BY database_name, schema_name, task_name
        HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
    """)


def data_transfer_drivers() -> pd.DataFrame:
    """T01: Data transfer cost drivers."""
    cols = ["object_name", "tb_transferred"]
    if not _has_table("data_transfer_history"):
        return pd.DataFrame(columns=cols)
    return _query("""
        SELECT transfer_type || ':' || source_cloud || '/' ||
               source_region || '->' || target_cloud || '/' ||
               target_region AS object_name,
               ROUND(SUM(bytes_transferred) / POWER(1024, 4), 3)
                   AS tb_transferred
        FROM data_transfer_history
        GROUP BY transfer_type, source_cloud, source_region,
                 target_cloud, target_region
        HAVING SUM(bytes_transferred) > 0
        ORDER BY SUM(bytes_transferred) DESC
    """)


def executive_cost_trend() -> pd.DataFrame:
    """F01: Daily spend by service type."""
    return _query(f"""
        SELECT service_type AS object_name,
               usage_date AS date,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM metering_daily_history
        GROUP BY usage_date, service_type
        ORDER BY usage_date, SUM(credits_used) DESC
    """)


def all_service_cost_profile() -> pd.DataFrame:
    """M01: All service type costs."""
    return _query(f"""
        SELECT service_type AS object_name,
               ROUND(SUM(credits_used), 2) AS credits,
               ROUND(SUM(credits_used) * {CREDIT_PRICE}, 2) AS cost_usd
        FROM metering_daily_history
        GROUP BY service_type
        HAVING SUM(credits_used) > 0
        ORDER BY SUM(credits_used) DESC
    """)


def cost_breakdown_monthly(months: int = 12) -> pd.DataFrame:
    """Monthly cost breakdown by service category for the last N months.

    Uses the same sources and CASE mapping as cost_breakdown() — same three
    tables, same service type lists — so the current-month bar is consistent
    with the pie chart. Returns columns: month (YYYY-MM str), category, cost_usd.
    """
    con = _con()
    try:
        cutoff = f"DATE_TRUNC('month', {_as_of_sql()} - INTERVAL {months} MONTH)"

        # Warehouse metering — AI warehouses → AI & ML, rest → Managed Compute
        wh = _coerce_decimal_columns(con.execute(f"""
            SELECT DATE_TRUNC('month', start_time) AS month,
                   CASE WHEN warehouse_name IN
                     ('ML_TRAINING','CORTEX_AI','CORTEX_SEARCH','CORTEX_AGENTS')
                     THEN 'AI & ML' ELSE 'Managed Compute'
                   END AS category,
                   SUM(credits_used) * {CREDIT_PRICE} AS cost_usd
            FROM warehouse_metering_history
            WHERE start_time >= {cutoff}
            GROUP BY month, category
        """).fetchdf())

        # Serverless metering — comprehensive mapping matching cost_breakdown()
        svc = _coerce_decimal_columns(con.execute(f"""
            SELECT DATE_TRUNC('month', start_time) AS month,
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
                       'OPENFLOW_COMPUTE_BYOC','OPENFLOW_COMPUTE_SNOWFLAKE',
                       'POSTGRES_COMPUTE','POSTGRES_COMPUTE_HA',
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
            FROM metering_history
            WHERE start_time >= {cutoff}
            GROUP BY month, category
        """).fetchdf())

        # Storage — monthly average of daily snapshots
        storage = _coerce_decimal_columns(con.execute(f"""
            SELECT DATE_TRUNC('month', usage_date) AS month,
                   'Storage' AS category,
                   AVG((storage_bytes + stage_bytes + failsafe_bytes)
                       / POWER(1024, 4)) * 23.0 AS cost_usd
            FROM storage_usage
            WHERE usage_date >= {cutoff}
            GROUP BY month
        """).fetchdf())

        df = pd.concat([wh, svc, storage], ignore_index=True)
        if df.empty:
            return pd.DataFrame()

        df = _coerce_decimal_columns(df)
        df["cost_usd"] = pd.to_numeric(df["cost_usd"], errors="coerce").fillna(0.0)
        df["month"] = _as_naive_timestamp(df["month"])
        df = df.groupby(["month", "category"], as_index=False)["cost_usd"].sum()
        df["cost_usd"] = df["cost_usd"].round(2)
        df["month"] = df["month"].dt.strftime("%Y-%m")
        df = df.sort_values(["month", "category"])
        return df
    finally:
        con.close()


def leaderboard_snapshot() -> dict[str, Any]:
    """All LeaderBoard queries in one shot — mirrors live_data.leaderboard_snapshot."""
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
