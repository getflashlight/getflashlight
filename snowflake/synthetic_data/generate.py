"""Generate synthetic Snowflake ACCOUNT_USAGE datasets for a ≤$600k/year demo account.

Produces one Parquet file per source view, modeling a mixed-workload account:
  - 16 warehouses across ETL, BI, ML/AI, dev, streaming (all cost services retained)
  - ~144K credits/year (~12K/month at $4/credit) so realized TCO stays ≤ $50K/month
  - AI workloads (Cortex functions, ML training), serverless, storage, transfer

Run with ``uv run python -c "import runpy; runpy.run_path('snowflake/synthetic_data/generate.py')['main']()"``
from the repository root (or ``fl sample`` once the sample hook is wired).
"""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from flashlight.core.logging import get_logger

OUTPUT_DIR = Path(__file__).parent
SEED = 42
random.seed(SEED)
np.random.seed(SEED)
logger = get_logger("flashlight.sample.snowflake")


def _log_written(dataset: str, rows: int, **extra: object) -> None:
    """Emit one structlog line per dataset — same shape as bronze/gold sample logs."""
    logger.info("snowflake_synthetic_written", dataset=dataset, rows=rows, **extra)


# --- Account parameters ---
ACCOUNT = "ACME_ANALYTICS"
START_DATE = date(2026, 1, 1)
END_DATE = date(2026, 8, 8)  # matches the validated reference window
DAYS = (END_DATE - START_DATE).days + 1  # 220 days
CREDIT_PRICE = 4.00  # $/credit
# Hard ceilings: monthly TCO ≤ $50K, annual ≤ $600K. Credit budget is sized under the
# reference $4.032M / 84K-credit profile so realized TCO (WH + metering + storage)
# stays inside those caps.
MONTHLY_COST_CAP = 50_000
ANNUAL_COST_CAP = 600_000
ANNUAL_COST_TARGET = 576_000  # $48K/mo × 12 — headroom under the $50K / $600K caps
MONTHLY_CREDITS = ANNUAL_COST_TARGET / (12 * CREDIT_PRICE)  # 12,000
# Prior reference demo was 84K credits/mo ($4.032M/yr). Absolute waste/storage sizes
# scale with this ratio so every cost service is retained, only scaled.
_COST_SCALE = MONTHLY_CREDITS / 84_000
# Note: actual generated credits vary due to hourly traffic patterns;
# the dashboard shows the realized credits, not the target.
# Hidden Waste KPI target: share of last-30-day spend (validated ~33% at full
# _COST_SCALE; dialed to 23% for a more plausible actionable-savings headline).
HIDDEN_WASTE_PCT_TARGET = 0.23

# --- Warehouse definitions ---
# (name, size, credits_per_hour, workload_type, pct_of_total)
# AI workloads (ml + ai) ~20% of warehouse spend; + 5% serverless = ~25-30% total
WAREHOUSES = [
    ("ETL_PROD", "X-Large", 16, "etl", 0.20),
    ("DBT_PROD", "Large", 8, "etl", 0.12),
    ("BI_REPORTS", "Medium", 4, "bi", 0.10),
    ("LOOKER", "Medium", 4, "bi", 0.07),
    ("ANALYTICS", "Large", 8, "analytics", 0.08),
    ("DATA_SCIENCE", "Large", 8, "data_science", 0.06),
    ("ML_TRAINING", "2X-Large", 32, "ml", 0.08),
    ("CORTEX_AI", "Large", 8, "ai", 0.07),
    ("CORTEX_SEARCH", "Medium", 4, "ai", 0.03),
    ("CORTEX_AGENTS", "Medium", 4, "ai", 0.02),
    ("STREAMING", "Medium", 4, "streaming", 0.04),
    ("FINANCE", "Small", 2, "bi", 0.03),
    ("MARKETING", "Medium", 4, "bi", 0.03),
    ("AIRFLOW", "Medium", 4, "etl", 0.02),
    ("DEV", "Small", 2, "dev", 0.03),
    ("ADHOC", "Small", 2, "dev", 0.02),
]

# --- User profiles: role-based warehouse access + usage patterns ---
# pattern: "good" = efficient, "medium" = normal, "bad" = wasteful (drives hidden waste)
USER_PROFILES = {
    "ETL_SERVICE": {"warehouses": ["ETL_PROD", "DBT_PROD"], "pattern": "good", "type": "compute"},
    "DBT_RUNNER": {"warehouses": ["DBT_PROD", "ETL_PROD"], "pattern": "good", "type": "compute"},
    "LOOKER_SVC": {"warehouses": ["LOOKER", "BI_REPORTS"], "pattern": "good", "type": "compute"},
    "ANALYST_JANE": {"warehouses": ["ANALYTICS", "BI_REPORTS", "FINANCE"], "pattern": "medium", "type": "compute"},
    "ANALYST_BOB": {"warehouses": ["ANALYTICS", "BI_REPORTS"], "pattern": "good", "type": "compute"},
    "DS_TEAM": {"warehouses": ["DATA_SCIENCE", "ML_TRAINING"], "pattern": "medium", "type": "ai"},
    "ML_PIPELINE": {"warehouses": ["ML_TRAINING", "CORTEX_AI"], "pattern": "good", "type": "ai"},
    "CORTEX_SVC": {"warehouses": ["CORTEX_AI", "CORTEX_SEARCH", "CORTEX_AGENTS"], "pattern": "medium", "type": "ai"},
    "AIRFLOW_SVC": {"warehouses": ["ETL_PROD", "DBT_PROD", "AIRFLOW"], "pattern": "good", "type": "compute"},
    "FINANCE_RPT": {"warehouses": ["FINANCE", "BI_REPORTS"], "pattern": "medium", "type": "compute"},
    "MARKETING_USER": {"warehouses": ["MARKETING", "ADHOC"], "pattern": "bad", "type": "compute"},
    "DEV_ALICE": {"warehouses": ["DEV", "ADHOC", "DATA_SCIENCE"], "pattern": "bad", "type": "compute"},
    "DEV_CHARLIE": {"warehouses": ["DEV", "ADHOC", "CORTEX_AI"], "pattern": "bad", "type": "ai"},
    "STREAMING_SVC": {"warehouses": ["STREAMING"], "pattern": "good", "type": "compute"},
    "ADHOC_USER": {"warehouses": ["ADHOC", "DEV", "ANALYTICS"], "pattern": "bad", "type": "compute"},
}

# Build reverse lookup: warehouse -> list of users who can access it
_WH_USERS: dict[str, list[str]] = {}
for _user, _prof in USER_PROFILES.items():
    for _wh in _prof["warehouses"]:
        _WH_USERS.setdefault(_wh, []).append(_user)


def _hours_in_day() -> int:
    return 24


def generate_warehouse_metering_history() -> pd.DataFrame:
    """Credit consumption per warehouse per hour for 30 days."""
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        is_weekday = usage_date.weekday() < 5
        # Monthly growth: ~1.5%/month increase (≈$700/month at ~$48K base)
        months_elapsed = day_offset / 30.0
        growth_factor = 1.0 + (months_elapsed * 0.015)
        for wh_name, size, cph, wtype, pct in WAREHOUSES:
            daily_budget = (MONTHLY_CREDITS * pct) / 30  # per-month budget / 30 days
            for hour in range(24):
                # Traffic pattern: peak 8-18 for BI/analytics, flat for ETL/streaming
                if wtype in ("etl", "streaming"):
                    hour_weight = 1.0 if 2 <= hour <= 8 else 0.3
                elif wtype in ("bi", "analytics"):
                    hour_weight = 1.5 if (9 <= hour <= 17 and is_weekday) else 0.2
                elif wtype in ("ml", "ai", "data_science"):
                    hour_weight = 1.2 if 6 <= hour <= 22 else 0.5
                else:  # dev, adhoc
                    hour_weight = 1.0 if (10 <= hour <= 16 and is_weekday) else 0.1

                credits = (daily_budget / 24) * hour_weight * growth_factor * np.random.uniform(0.7, 1.3)
                credits = max(0, credits)
                cloud_credits = credits * 0.1 * np.random.uniform(0.8, 1.2)

                rows.append({
                    "start_time": datetime(usage_date.year, usage_date.month, usage_date.day, hour),
                    "end_time": datetime(usage_date.year, usage_date.month, usage_date.day, hour, 59, 59),
                    "warehouse_name": wh_name,
                    "warehouse_id": hash(wh_name) % 10000 + 1,
                    "credits_used": round(credits, 4),
                    "credits_used_compute": round(credits * 0.9, 4),
                    "credits_used_cloud_services": round(cloud_credits, 4),
                })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "warehouse_metering_history.parquet")
    _log_written(
        "warehouse_metering_history",
        len(df),
        credits=int(round(float(df["credits_used"].sum()))),
    )
    return df


def generate_warehouse_load_history() -> pd.DataFrame:
    """Warehouse load metrics per 5-min interval, summarized to hourly for demo."""
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        is_weekday = usage_date.weekday() < 5
        for wh_name, size, cph, wtype, pct in WAREHOUSES:
            for hour in range(24):
                if wtype in ("etl", "streaming"):
                    avg_running = np.random.uniform(0.4, 0.9) if 2 <= hour <= 8 else np.random.uniform(0.02, 0.15)
                elif wtype in ("bi", "analytics"):
                    avg_running = np.random.uniform(0.3, 0.7) if (9 <= hour <= 17 and is_weekday) else np.random.uniform(0.01, 0.1)
                elif wtype in ("ml", "ai"):
                    avg_running = np.random.uniform(0.5, 0.95) if 6 <= hour <= 22 else np.random.uniform(0.1, 0.3)
                else:
                    avg_running = np.random.uniform(0.05, 0.25) if is_weekday else np.random.uniform(0.0, 0.05)

                # Some warehouses get queue pressure
                avg_queued = 0.0
                if wh_name in ("ETL_PROD", "ML_TRAINING", "BI_REPORTS") and avg_running > 0.7:
                    avg_queued = np.random.uniform(0.0, 0.3)

                rows.append({
                    "start_time": datetime(usage_date.year, usage_date.month, usage_date.day, hour),
                    "end_time": datetime(usage_date.year, usage_date.month, usage_date.day, hour, 59, 59),
                    "warehouse_name": wh_name,
                    "avg_running": round(avg_running, 3),
                    "avg_queued_load": round(avg_queued, 3),
                    "avg_queued_provisioning": round(np.random.uniform(0, 0.02), 3),
                    "avg_blocked": round(np.random.uniform(0, 0.01), 3),
                })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "warehouse_load_history.parquet")
    _log_written("warehouse_load_history", len(df))
    return df


def generate_query_history() -> pd.DataFrame:
    """~200K queries over 30 days with realistic role-based patterns."""
    query_types = ["SELECT", "INSERT", "CREATE_TABLE_AS_SELECT", "MERGE", "COPY", "CALL"]
    roles_by_type = {
        "compute": ["ETL_ROLE", "ANALYST_ROLE", "SYSADMIN"],
        "ai": ["DATA_SCIENCE_ROLE", "ML_ROLE", "AI_ROLE"],
        "etl": ["ETL_ROLE", "SYSADMIN"],
        "bi": ["ANALYST_ROLE", "SYSADMIN"],
        "analytics": ["ANALYST_ROLE", "DATA_SCIENCE_ROLE"],
        "ml": ["ML_ROLE", "DATA_SCIENCE_ROLE"],
        "data_science": ["DATA_SCIENCE_ROLE", "ML_ROLE"],
        "streaming": ["ETL_ROLE", "SYSADMIN"],
        "dev": ["PUBLIC", "ANALYST_ROLE", "SYSADMIN"],
    }

    # Pre-generate query hashes (patterns)
    n_patterns = 800
    patterns = [hashlib.md5(f"pattern_{i}".encode()).hexdigest()[:32] for i in range(n_patterns)]

    rows = []
    queries_per_day = 7000  # ~210K over 30 days

    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        is_weekday = usage_date.weekday() < 5
        day_queries = int(queries_per_day * (1.2 if is_weekday else 0.6))

        for _ in range(day_queries):
            wh_idx = random.choices(range(len(WAREHOUSES)), weights=[w[4] for w in WAREHOUSES])[0]
            wh_name = WAREHOUSES[wh_idx][0]
            wtype = WAREHOUSES[wh_idx][3]

            hour = random.choices(range(24), weights=[
                max(0.1, 1.0 if 8 <= h <= 18 else 0.3) for h in range(24)
            ])[0]

            # Query characteristics based on workload type
            if wtype == "etl":
                qt = random.choice(["INSERT", "CREATE_TABLE_AS_SELECT", "MERGE", "COPY"])
                elapsed = int(np.random.lognormal(10, 1.5))  # ms
                scanned = int(np.random.lognormal(28, 2))
                spill_local = int(np.random.lognormal(24, 3)) if random.random() < 0.15 else 0
                spill_remote = int(np.random.lognormal(26, 2)) if random.random() < 0.05 else 0
                cache_pct = np.random.uniform(0, 40)
            elif wtype in ("bi", "analytics"):
                qt = "SELECT"
                elapsed = int(np.random.lognormal(8, 1.2))
                scanned = int(np.random.lognormal(25, 2.5))
                spill_local = int(np.random.lognormal(22, 2)) if random.random() < 0.08 else 0
                spill_remote = 0
                cache_pct = np.random.uniform(30, 90)
            elif wtype in ("ml", "ai", "data_science"):
                qt = random.choice(["SELECT", "CALL", "INSERT"])
                elapsed = int(np.random.lognormal(11, 1.8))
                scanned = int(np.random.lognormal(29, 2))
                spill_local = int(np.random.lognormal(26, 2)) if random.random() < 0.25 else 0
                spill_remote = int(np.random.lognormal(28, 1.5)) if random.random() < 0.10 else 0
                cache_pct = np.random.uniform(5, 50)
            else:  # dev/adhoc
                qt = random.choice(["SELECT", "INSERT", "SELECT", "SELECT"])
                elapsed = int(np.random.lognormal(7, 1.5))
                scanned = int(np.random.lognormal(22, 3))
                spill_local = 0
                spill_remote = 0
                cache_pct = np.random.uniform(40, 95)

            query_id = str(uuid.uuid4())
            # Select user based on warehouse access
            user_name = random.choice(_WH_USERS.get(wh_name, list(USER_PROFILES.keys())))
            user_pattern = USER_PROFILES.get(user_name, {}).get("pattern", "medium")

            # "bad" users degrade query characteristics
            if user_pattern == "bad":
                elapsed = int(elapsed * random.uniform(1.5, 3.0))
                cache_pct = max(0, cache_pct * 0.4)
                spill_local = int(spill_local * 2.5) if spill_local else int(np.random.lognormal(24, 2))
                spill_remote = int(spill_remote * 2.0) if spill_remote else (int(np.random.lognormal(26, 1.5)) if random.random() < 0.15 else 0)
            elif user_pattern == "good":
                elapsed = int(elapsed * random.uniform(0.5, 0.8))
                cache_pct = min(100, cache_pct * 1.3)
                spill_local = 0
                spill_remote = 0

            start_time = datetime(usage_date.year, usage_date.month, usage_date.day, hour,
                                  random.randint(0, 59), random.randint(0, 59))
            compilation_time = int(np.random.lognormal(6, 1))
            queued_time = int(np.random.exponential(500)) if random.random() < 0.1 else 0

            rows.append({
                "query_id": query_id,
                "query_hash": random.choice(patterns),
                "query_parameterized_hash": random.choice(patterns[:400]),
                "query_text": f"/* {wtype} workload */ SELECT ... FROM ...",
                "query_type": qt,
                "query_tag": random.choice([f"team:{wtype}", f"pipeline:{wtype}_daily", "", "cortex_inference"]) if wtype == "ai" else random.choice([f"team:{wtype}", f"pipeline:{wtype}", ""]),
                "database_name": random.choice(["RAW", "ANALYTICS", "ML_FEATURES", "REPORTING", "STAGING"]),
                "schema_name": random.choice(["PUBLIC", "CORE", "MARTS", "STAGING"]),
                "warehouse_name": wh_name,
                "warehouse_size": WAREHOUSES[wh_idx][1],
                "user_name": user_name,
                "role_name": random.choice(roles_by_type.get(wtype, roles_by_type["compute"])),
                "execution_status": "SUCCESS" if random.random() < 0.97 else random.choice(["FAIL", "INCIDENT_QUEUE_FULL"]),
                "start_time": start_time,
                "end_time": start_time + timedelta(milliseconds=elapsed),
                "total_elapsed_time": elapsed,
                "execution_time": max(0, elapsed - compilation_time - queued_time),
                "compilation_time": compilation_time,
                "queued_overload_time": queued_time,
                "queued_provisioning_time": 0,
                "queued_repair_time": 0,
                "transaction_blocked_time": 0,
                "bytes_scanned": max(0, scanned),
                "bytes_written": int(scanned * 0.3) if qt != "SELECT" else 0,
                "bytes_spilled_to_local_storage": max(0, spill_local),
                "bytes_spilled_to_remote_storage": max(0, spill_remote),
                "rows_produced": int(np.random.lognormal(8, 3)),
                "percentage_scanned_from_cache": round(min(100, max(0, cache_pct)), 1),
                "bytes_read_from_result": int(scanned * 0.5) if cache_pct > 80 and random.random() < 0.3 else 0,
                "partitions_scanned": int(np.random.lognormal(4, 2)),
                "partitions_total": int(np.random.lognormal(6, 2)),
            })

    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "query_history.parquet")
    _log_written("query_history", len(df))
    return df


def generate_query_attribution_history(query_df: pd.DataFrame) -> pd.DataFrame:
    """Attributed credits per query — subset of successful queries."""
    successful = query_df[query_df["execution_status"] == "SUCCESS"].copy()

    # Distribute monthly credits across queries proportional to elapsed time
    total_elapsed = successful["total_elapsed_time"].sum()
    successful["credits_attributed_compute"] = (
        successful["total_elapsed_time"] / total_elapsed * MONTHLY_CREDITS
    ).round(6)
    successful["credits_used_query_acceleration"] = np.where(
        successful["bytes_scanned"] > 1e11,
        successful["credits_attributed_compute"] * np.random.uniform(0.05, 0.2, len(successful)),
        0,
    ).round(6)

    result = successful[["query_id", "query_parameterized_hash", "warehouse_name",
                         "start_time", "credits_attributed_compute",
                         "credits_used_query_acceleration"]].copy()
    pq.write_table(pa.Table.from_pandas(result), OUTPUT_DIR / "query_attribution_history.parquet")
    _log_written(
        "query_attribution_history",
        len(result),
        credits=int(round(float(result["credits_attributed_compute"].sum()))),
    )
    return result


def generate_storage_usage() -> pd.DataFrame:
    """Daily account storage — scaled with spend (~10% of TCO at $23/TB)."""
    rows = []
    base_storage = 900.0 * _COST_SCALE * (1024**4)  # ~253 TB
    base_stage = 120.0 * _COST_SCALE * (1024**4)  # ~34 TB stages
    base_failsafe = 60.0 * _COST_SCALE * (1024**4)  # ~17 TB failsafe

    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        growth = 1 + (day_offset * 0.002)  # 0.2% daily growth
        rows.append({
            "usage_date": usage_date,
            "storage_bytes": int(base_storage * growth * np.random.uniform(0.99, 1.01)),
            "stage_bytes": int(base_stage * growth * np.random.uniform(0.95, 1.05)),
            "failsafe_bytes": int(base_failsafe * np.random.uniform(0.98, 1.02)),
            "hybrid_table_storage_bytes": int(20 * _COST_SCALE * (1024**4) * np.random.uniform(0.9, 1.1)),
            "archive_storage_cool_bytes": int(40 * _COST_SCALE * (1024**4) * growth),
            "archive_storage_cold_bytes": int(20 * _COST_SCALE * (1024**4) * growth),
        })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "storage_usage.parquet")
    _log_written("storage_usage", len(df))
    return df


def generate_table_storage_metrics() -> pd.DataFrame:
    """Top 200 tables by storage. Realistic range: 100 MB to ~140 TB max."""
    databases = ["RAW", "ANALYTICS", "ML_FEATURES", "REPORTING", "STAGING"]
    schemas = ["PUBLIC", "CORE", "MARTS", "STAGING", "ML", "FEATURES"]
    rows = []
    # Cap scales with account size (was 500 TB at $4M)
    max_bytes = int(500 * _COST_SCALE * (1024 ** 4))
    for i in range(200):
        # lognormal scaled so table sizes track the smaller account
        active = min(int(np.random.lognormal(25, 1.8) * _COST_SCALE), max_bytes)
        rows.append({
            "table_catalog": random.choice(databases),
            "table_schema": random.choice(schemas),
            "table_name": f"TABLE_{i:04d}" if i > 20 else random.choice([
                "FACT_ORDERS", "DIM_CUSTOMERS", "FACT_EVENTS", "ML_EMBEDDINGS",
                "RAW_CLICKSTREAM", "CORTEX_INFERENCE_LOG", "FEATURE_STORE",
                "DIM_PRODUCTS", "FACT_TRANSACTIONS", "RAW_API_LOGS",
                "STAGING_IMPORTS", "AGG_DAILY_METRICS", "USER_SESSIONS",
                "ML_TRAINING_DATA", "CORTEX_SEARCH_INDEX", "RAW_IOT_TELEMETRY",
                "DIM_GEOGRAPHY", "FACT_REVENUE", "RAW_SOCIAL_FEEDS", "AUDIT_LOG",
                "VECTOR_EMBEDDINGS",
            ]),
            "active_bytes": active,
            "time_travel_bytes": int(active * np.random.uniform(0.05, 0.3)),
            "failsafe_bytes": int(active * np.random.uniform(0.02, 0.15)),
            "retained_for_clone_bytes": int(active * np.random.uniform(0, 0.1)) if random.random() < 0.3 else 0,
            "is_transient": random.random() < 0.15,
        })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "table_storage_metrics.parquet")
    _log_written("table_storage_metrics", len(df))
    return df


def generate_tag_references() -> pd.DataFrame:
    """Tags on warehouses + databases — 90% spend attributed via 5 required tags.

    Models a governance-mature org where:
      - ~90% of warehouse spend is fully tagged (5/5 required tags)
      - ~10% of spend is unattributed (untagged or partially tagged)
      - Small quality issues exist in some tag values
      - Databases have moderate coverage
    Required tags: department, environment, application, owner, cost_center
    """
    rows = []

    # Tag taxonomy
    departments = ["Engineering", "Data", "Finance", "Marketing", "Operations", "IT"]
    environments = ["prod", "staging", "dev", "sandbox"]
    applications = ["Analytics Platform", "ML Pipeline", "ERP Integration",
                    "Customer 360", "Real-time Streaming", "BI Reporting"]

    # Workload type → department mapping (realistic attribution)
    wtype_dept = {
        "etl": "Data", "bi": "Finance", "analytics": "Data",
        "data_science": "Engineering", "ml": "Engineering", "ai": "Engineering",
        "streaming": "Data", "dev": "Engineering",
    }
    wtype_env = {
        "etl": "prod", "bi": "prod", "analytics": "prod",
        "data_science": "staging", "ml": "prod", "ai": "prod",
        "streaming": "prod", "dev": "dev",
    }
    wtype_app = {
        "etl": "ERP Integration", "bi": "BI Reporting", "analytics": "Analytics Platform",
        "data_science": "ML Pipeline", "ml": "ML Pipeline", "ai": "Customer 360",
        "streaming": "Real-time Streaming", "dev": "Analytics Platform",
    }

    # Sort warehouses by spend percentage (descending) to control which are untagged
    # The bottom ~10% by spend will be untagged/partially tagged
    wh_sorted = sorted(WAREHOUSES, key=lambda x: x[4], reverse=True)
    cumulative_pct = 0.0

    for wh_name, _, _, wtype, pct in wh_sorted:
        cumulative_pct += pct
        if cumulative_pct <= 0.90:
            # Top 90% of spend: fully tagged with 5 required tags
            tags_to_add = ["department", "environment", "application", "owner", "cost_center"]
        elif cumulative_pct <= 0.95:
            # Next 5%: partially tagged (1-3 tags) — governance gap
            tags_to_add = random.sample(
                ["department", "environment", "application", "owner", "cost_center"],
                k=random.randint(1, 3),
            )
        else:
            # Bottom 5%: completely untagged — generates unattributed findings
            continue

        for tag_name in tags_to_add:
            # Introduce quality issues for ~10% of tags (G04 findings)
            quality_roll = random.random()
            if quality_roll < 0.04:
                tag_value = ""  # Empty value
            elif quality_roll < 0.07:
                tag_value = random.choice(["TBD", "TODO", "unknown"])  # Placeholder
            elif quality_roll < 0.10 and tag_name == "environment":
                # Case mismatch (e.g. "Prod" instead of "prod")
                tag_value = wtype_env.get(wtype, "prod").capitalize()
            else:
                # Good value
                if tag_name == "department":
                    tag_value = wtype_dept.get(wtype, random.choice(departments))
                elif tag_name == "environment":
                    tag_value = wtype_env.get(wtype, "prod")
                elif tag_name == "application":
                    tag_value = wtype_app.get(wtype, random.choice(applications))
                elif tag_name == "owner":
                    tag_value = f"{wtype}_team"
                else:  # cost_center
                    tag_value = f"CC_{wtype.upper()}"

            rows.append({
                "object_name": wh_name,
                "domain": "WAREHOUSE",
                "tag_name": tag_name,
                "tag_value": tag_value,
            })

    # Database-level tags (lower coverage than warehouses — common in real orgs)
    databases = ["ANALYTICS", "ML_FEATURES", "REPORTING", "RAW_EVENTS",
                 "STAGING", "SANDBOX", "PRODUCTION"]
    for db_name in databases:
        if random.random() < 0.45:  # Only 45% of databases tagged at all
            for tag_name in random.sample(
                ["department", "environment", "owner"], k=random.randint(1, 3)
            ):
                if tag_name == "department":
                    tag_value = random.choice(departments)
                elif tag_name == "environment":
                    tag_value = random.choice(environments)
                else:
                    tag_value = random.choice(["platform_team", "data_eng", "analytics_team"])
                rows.append({
                    "object_name": db_name,
                    "domain": "DATABASE",
                    "tag_name": tag_name,
                    "tag_value": tag_value,
                })

    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "tag_references.parquet")

    # Print governance stats
    wh_names = {wh[0] for wh in WAREHOUSES}
    tagged_wh = df[df["domain"] == "WAREHOUSE"]["object_name"].nunique()
    fully_tagged = df[df["domain"] == "WAREHOUSE"].groupby("object_name")["tag_name"].nunique()
    full_count = (fully_tagged >= 5).sum()
    _log_written(
        "tag_references",
        len(df),
        warehouses_tagged=f"{tagged_wh}/{len(wh_names)}",
        fully_tagged=int(full_count),
    )
    return df


def generate_warehouse_events_history() -> pd.DataFrame:
    """Warehouse suspend/resume events — some with thrashing patterns."""
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for wh_name, _, _, wtype, _ in WAREHOUSES:
            # DEV and ADHOC thrash (many suspend/resume cycles)
            if wtype == "dev":
                n_events = random.randint(8, 20)
            elif wtype in ("bi", "analytics"):
                n_events = random.randint(3, 8)
            else:
                n_events = random.randint(1, 4)

            for _ in range(n_events):
                hour = random.randint(0, 23)
                rows.append({
                    "timestamp": datetime(usage_date.year, usage_date.month, usage_date.day, hour, random.randint(0, 59)),
                    "warehouse_name": wh_name,
                    "event_name": random.choice(["RESUME_WAREHOUSE", "SUSPEND_WAREHOUSE"]),
                    "event_reason": random.choice(["SUSPEND_IDLE", "RESUME_QUERY", "RESUME_USER"]),
                    "event_state": random.choice(["STARTED", "COMPLETED"]),
                    "cluster_number": 1,
                })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "warehouse_events_history.parquet")
    _log_written("warehouse_events_history", len(df))
    return df


def generate_metering_history() -> pd.DataFrame:
    """All serverless service metering — AI + managed services (pipes, tasks, clustering, etc.)."""
    # AI services (~10% of monthly credits)
    ai_monthly_credits = MONTHLY_CREDITS * 0.10
    ai_services = [
        ("CORTEX_AI_FUNCTIONS", "CORTEX_AI", 0.35),
        ("CORTEX_SEARCH", "CORTEX_SEARCH_SVC", 0.20),
        ("AI_SERVICES", "ML_TRAINING_SVC", 0.15),
        ("CORTEX_ANALYST", "ANALYST_SVC", 0.10),
        ("DOCUMENT_AI", "DOC_AI_SVC", 0.08),
        ("SNOWFLAKE_INTELLIGENCE", "INTELLIGENCE_SVC", 0.05),
        ("CORTEX_AGENTS", "AGENT_SVC", 0.04),
        ("CORTEX_GUARDRAILS", "GUARDRAILS_SVC", 0.03),
    ]
    # Managed services (~12% of monthly credits — notable cost visible to leadership)
    managed_monthly_credits = MONTHLY_CREDITS * 0.12
    managed_services = [
        ("AUTOMATIC_CLUSTERING", "AUTO_CLUSTER_SVC", 0.22),
        ("SNOWPIPE", "PIPE_SVC", 0.18),
        ("SERVERLESS_TASK", "TASK_SVC", 0.16),
        ("REPLICATION", "REPLICATION_SVC", 0.14),
        ("DATA_TRANSFER", "EGRESS_SVC", 0.12),
        ("SEARCH_OPTIMIZATION", "SEARCH_OPT_SVC", 0.08),
        ("MATERIALIZED_VIEW", "MATVIEW_SVC", 0.05),
        ("QUERY_ACCELERATION", "QAS_SVC", 0.05),
    ]
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for svc_type, name, pct in ai_services:
            daily_credits = (ai_monthly_credits * pct) / 30
            credits = daily_credits * np.random.uniform(0.7, 1.3)
            rows.append({
                "start_time": datetime(usage_date.year, usage_date.month, usage_date.day),
                "end_time": datetime(usage_date.year, usage_date.month, usage_date.day, 23, 59),
                "service_type": svc_type,
                "name": name,
                "entity_type": "SERVICE",
                "database_name": random.choice(["ANALYTICS", "ML_FEATURES", "REPORTING"]),
                "schema_name": "PUBLIC",
                "credits_used": round(credits, 4),
            })
        for svc_type, name, pct in managed_services:
            daily_credits = (managed_monthly_credits * pct) / 30
            credits = daily_credits * np.random.uniform(0.7, 1.3)
            rows.append({
                "start_time": datetime(usage_date.year, usage_date.month, usage_date.day),
                "end_time": datetime(usage_date.year, usage_date.month, usage_date.day, 23, 59),
                "service_type": svc_type,
                "name": name,
                "entity_type": "SERVICE",
                "database_name": random.choice(["RAW", "ANALYTICS", "REPORTING", "ML_FEATURES"]),
                "schema_name": "PUBLIC",
                "credits_used": round(credits, 4),
            })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "metering_history.parquet")
    total_ai = df[df["service_type"].isin([s[0] for s in ai_services])]["credits_used"].sum()
    total_managed = df[df["service_type"].isin([s[0] for s in managed_services])]["credits_used"].sum()
    _log_written(
        "metering_history",
        len(df),
        ai_credits=int(round(float(total_ai))),
        managed_credits=int(round(float(total_managed))),
    )
    return df


def generate_cortex_ai_functions_usage() -> pd.DataFrame:
    """Cortex AI function usage — for AI02 check."""
    functions = [
        ("COMPLETE", "llama3.1-70b", 0.25),
        ("COMPLETE", "mistral-large2", 0.20),
        ("COMPLETE", "claude-3.5-sonnet", 0.15),
        ("SUMMARIZE", "llama3.1-70b", 0.10),
        ("TRANSLATE", "snowflake-arctic", 0.08),
        ("SENTIMENT", "snowflake-arctic", 0.07),
        ("CLASSIFY_TEXT", "llama3.1-8b", 0.05),
        ("EXTRACT_ANSWER", "mistral-large2", 0.05),
        ("EMBED_TEXT_768", "e5-base-v2", 0.03),
        ("EMBED_TEXT_1024", "voyage-multilingual-2", 0.02),
    ]
    ai_func_credits = MONTHLY_CREDITS * 0.30 * 0.35  # 35% of AI budget
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for func_name, model, pct in functions:
            daily_credits = (ai_func_credits * pct) / 30
            credits = daily_credits * np.random.uniform(0.6, 1.4)
            calls = int(credits * np.random.uniform(50, 200))
            tokens_in = int(calls * np.random.uniform(200, 2000))
            tokens_out = int(calls * np.random.uniform(50, 500))
            rows.append({
                "start_time": datetime(usage_date.year, usage_date.month, usage_date.day),
                "function_name": func_name,
                "model_name": model,
                "credits": round(credits, 4),
                "calls": calls,
                "tokens_sent": tokens_in,
                "tokens_received": tokens_out,
                "user_id": random.choice(["CORTEX_SVC", "ML_PIPELINE", "ANALYST_JANE", "APP_SVC"]),
                "query_tag": random.choice(["inference:prod", "batch:daily", "interactive", ""]),
            })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "cortex_ai_functions_usage_history.parquet")
    _log_written("cortex_ai_functions_usage_history", len(df))
    return df


def generate_cortex_search_daily_usage() -> pd.DataFrame:
    """Cortex Search daily usage — for AI03 check."""
    services = [
        ("ANALYTICS", "SEARCH", "PRODUCT_SEARCH_SVC", 0.40),
        ("ML_FEATURES", "PUBLIC", "DOC_SEARCH_SVC", 0.35),
        ("REPORTING", "PUBLIC", "SUPPORT_SEARCH_SVC", 0.25),
    ]
    search_credits = MONTHLY_CREDITS * 0.30 * 0.20  # 20% of AI budget
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for db, schema, svc, pct in services:
            for ctype in ["SERVING", "EMBEDDING", "BATCH_QUERY"]:
                type_pct = {"SERVING": 0.5, "EMBEDDING": 0.35, "BATCH_QUERY": 0.15}[ctype]
                credits = (search_credits * pct * type_pct / 30) * np.random.uniform(0.7, 1.3)
                tokens = int(credits * np.random.uniform(5000, 20000))
                rows.append({
                    "usage_date": usage_date,
                    "database_name": db,
                    "schema_name": schema,
                    "service_name": svc,
                    "consumption_type": ctype,
                    "credits": round(credits, 4),
                    "tokens": tokens,
                })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "cortex_search_daily_usage_history.parquet")
    _log_written("cortex_search_daily_usage_history", len(df))
    return df


def generate_metering_daily_history() -> pd.DataFrame:
    """Daily metering by service type — for F01 executive trend."""
    service_types = [
        ("WAREHOUSE_METERING", 0.55),
        ("CLOUD_SERVICES", 0.06),
        ("AUTOMATIC_CLUSTERING", 0.04),
        ("SEARCH_OPTIMIZATION", 0.02),
        ("MATERIALIZED_VIEW", 0.02),
        ("SNOWPIPE", 0.03),
        ("SERVERLESS_TASK", 0.03),
        ("REPLICATION", 0.02),
        ("QUERY_ACCELERATION", 0.01),
        ("CORTEX_AI_FUNCTIONS", 0.06),
        ("CORTEX_SEARCH", 0.04),
        ("AI_SERVICES", 0.03),
        ("SNOWPARK_CONTAINER_SERVICES", 0.03),
        ("DOCUMENT_AI", 0.02),
        ("DATA_TRANSFER", 0.01),
        ("HYBRID_TABLE", 0.01),
        ("CORTEX_ANALYST", 0.01),
        ("CORTEX_AGENTS", 0.01),
    ]
    rows = []
    for day_offset in range(DAYS):
        usage_date = START_DATE + timedelta(days=day_offset)
        for svc, pct in service_types:
            daily = (MONTHLY_CREDITS * pct / 30) * np.random.uniform(0.7, 1.3)
            rows.append({
                "usage_date": usage_date,
                "service_type": svc,
                "credits_used": round(daily, 4),
            })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "metering_daily_history.parquet")
    _log_written("metering_daily_history", len(df))
    return df


def generate_serverless_services_data() -> None:
    """Generate Parquet for serverless services."""

    # Automatic clustering history
    tables = [
        ("ANALYTICS", "CORE", "FACT_ORDERS"),
        ("ANALYTICS", "CORE", "FACT_EVENTS"),
        ("RAW", "PUBLIC", "RAW_CLICKSTREAM"),
        ("ML_FEATURES", "FEATURES", "FEATURE_STORE"),
        ("REPORTING", "MARTS", "AGG_DAILY_METRICS"),
    ]
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for db, schema, table in tables:
            credits = np.random.uniform(0.5, 8.0)
            rows.append({
                "start_time": datetime(dt.year, dt.month, dt.day),
                "end_time": datetime(dt.year, dt.month, dt.day, 23, 59),
                "database_name": db, "schema_name": schema, "table_name": table,
                "credits_used": round(credits, 4),
                "num_bytes_reclustered": int(np.random.lognormal(28, 1.5)),
                "num_rows_reclustered": int(np.random.lognormal(18, 2)),
            })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "automatic_clustering_history.parquet")
    _log_written("automatic_clustering_history", len(df))

    # Snowpipe usage
    pipes = [
        ("RAW_EVENTS_PIPE", 0.35), ("CLICKSTREAM_PIPE", 0.25),
        ("IOT_TELEMETRY_PIPE", 0.20), ("<internal_or_auto_refresh>", 0.10),
        ("API_LOGS_PIPE", 0.10),
    ]
    pipe_credits = MONTHLY_CREDITS * 0.03
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for pipe_name, pct in pipes:
            credits = (pipe_credits * pct / 30) * np.random.uniform(0.7, 1.3)
            files = int(np.random.uniform(100, 5000))
            bytes_ins = int(files * np.random.uniform(1e6, 50e6))
            rows.append({
                "start_time": datetime(dt.year, dt.month, dt.day),
                "pipe_name": pipe_name,
                "credits_used": round(credits, 4),
                "bytes_inserted": bytes_ins,
                "files_inserted": files,
            })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "pipe_usage_history.parquet")
    _log_written("pipe_usage_history", len(df))

    # Serverless tasks
    tasks = [
        ("ANALYTICS", "ORCHESTRATION", "REFRESH_DASHBOARDS", 0.25),
        ("RAW", "INGESTION", "LOAD_EXTERNAL_DATA", 0.20),
        ("ML_FEATURES", "ML", "FEATURE_PIPELINE", 0.20),
        ("ANALYTICS", "CORE", "AGGREGATE_METRICS", 0.15),
        ("REPORTING", "ALERTS", "ANOMALY_DETECTOR", 0.10),
        ("RAW", "MAINTENANCE", "CLEANUP_STAGING", 0.10),
    ]
    task_credits = MONTHLY_CREDITS * 0.03
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for db, schema, task_name, pct in tasks:
            credits = (task_credits * pct / 30) * np.random.uniform(0.6, 1.4)
            rows.append({
                "start_time": datetime(dt.year, dt.month, dt.day),
                "database_name": db, "schema_name": schema,
                "task_name": task_name,
                "task_id": hash(f"{db}.{schema}.{task_name}") % 100000,
                "credits_used": round(credits, 4),
            })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "serverless_task_history.parquet")
    _log_written("serverless_task_history", len(df))

    # Data transfer
    transfers = [
        ("COPY", "AWS", "us-east-1", "AWS", "eu-west-1", 0.40),
        ("REPLICATION", "AWS", "us-east-1", "AWS", "us-west-2", 0.30),
        ("UNLOAD", "AWS", "us-east-1", "AZURE", "eastus2", 0.20),
        ("STAGE", "AWS", "us-east-1", "GCP", "us-central1", 0.10),
    ]
    rows = []
    for day_offset in range(DAYS):
        dt = START_DATE + timedelta(days=day_offset)
        for ttype, sc, sr, tc, tr, pct in transfers:
            bytes_t = int(np.random.lognormal(33, 1.5) * pct)
            rows.append({
                "start_time": datetime(dt.year, dt.month, dt.day),
                "transfer_type": ttype, "source_cloud": sc,
                "source_region": sr, "target_cloud": tc,
                "target_region": tr, "bytes_transferred": bytes_t,
            })
    df = pd.DataFrame(rows)
    pq.write_table(pa.Table.from_pandas(df), OUTPUT_DIR / "data_transfer_history.parquet")
    _log_written("data_transfer_history", len(df))


def _spend_30d_usd() -> float:
    """Last-30-day credit spend (WH + serverless) — same basis as hidden_waste_summary."""
    as_of = END_DATE
    cutoff = pd.Timestamp(as_of) - pd.Timedelta(days=30)
    wh = pq.read_table(OUTPUT_DIR / "warehouse_metering_history.parquet").to_pandas()
    svc = pq.read_table(OUTPUT_DIR / "metering_history.parquet").to_pandas()
    wh_c = float(wh.loc[pd.to_datetime(wh["start_time"]) >= cutoff, "credits_used"].sum())
    svc_c = float(svc.loc[pd.to_datetime(svc["start_time"]) >= cutoff, "credits_used"].sum())
    return (wh_c + svc_c) * CREDIT_PRICE


def _scale_hidden_waste(
    compute: pd.DataFrame,
    storage: pd.DataFrame,
    ai: pd.DataFrame,
    *,
    target_pct: float = HIDDEN_WASTE_PCT_TARGET,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Scale waste $ so headline total ≈ target_pct of last-30-day spend."""
    spend_30d = max(_spend_30d_usd(), 1.0)
    compute_total = float(compute["wasted_cost_usd"].sum()) if not compute.empty else 0.0
    storage_annual = (
        float(storage["monthly_cost_usd"].sum()) * 12 if not storage.empty else 0.0
    )
    ai_total = float(ai["wasted_cost_usd"].sum()) if not ai.empty else 0.0
    current = compute_total + storage_annual + ai_total
    if current <= 0:
        return compute, storage, ai
    scale = (spend_30d * target_pct) / current
    compute = compute.copy()
    storage = storage.copy()
    ai = ai.copy()
    for col in ("actual_cost_usd", "wasted_credits", "wasted_cost_usd"):
        if col in compute.columns:
            compute[col] = (compute[col].astype(float) * scale).round(2)
    for col in ("actual_cost_usd", "monthly_cost_usd"):
        if col in storage.columns:
            storage[col] = (storage[col].astype(float) * scale).round(2)
    for col in ("actual_cost_usd", "wasted_credits", "wasted_cost_usd"):
        if col in ai.columns:
            ai[col] = (ai[col].astype(float) * scale).round(2)
    return compute, storage, ai


def generate_shadow_waste_data() -> None:
    """Generate shadow waste findings for Compute, Storage, and AI/Cortex."""
    # ── Compute Shadow Waste ───────────────────────────────────────────────
    rows = []
    for wh_name, size, cph, wtype, pct in WAREHOUSES:
        # Total warehouse cost for last 30 days
        total_credits = (MONTHLY_CREDITS * pct / 30) * 30 * 0.64  # avg hourly weight
        if wtype == "dev":
            idle_hours = int(np.random.uniform(200, 500) * _COST_SCALE)
            idle_credits = idle_hours * np.random.uniform(1.5, 3.0)
        elif wtype in ("bi", "analytics"):
            idle_hours = int(np.random.uniform(50, 150) * _COST_SCALE)
            idle_credits = idle_hours * np.random.uniform(2.0, 6.0)
        else:
            idle_hours = int(np.random.uniform(10, 60) * _COST_SCALE)
            idle_credits = idle_hours * np.random.uniform(3.0, 12.0)
        rows.append({
            "warehouse_name": wh_name, "waste_type": "IDLE_RUNNING",
            "idle_hours": idle_hours,
            "actual_cost_usd": round(total_credits * CREDIT_PRICE, 2),
            "wasted_credits": round(idle_credits, 2),
            "wasted_cost_usd": round(idle_credits * CREDIT_PRICE, 2),
            "recommendation": "Reduce auto-suspend timeout or schedule",
            "size": size,
        })
    for wh_name, size, cph, wtype, pct in WAREHOUSES:
        if wtype in ("dev", "bi") and random.random() < 0.5:
            total_credits = (MONTHLY_CREDITS * pct / 30) * 30 * 0.64
            over_credits = np.random.uniform(50, 300) * _COST_SCALE
            rows.append({
                "warehouse_name": wh_name, "waste_type": "OVERSIZED",
                "idle_hours": 0,
                "actual_cost_usd": round(total_credits * CREDIT_PRICE, 2),
                "wasted_credits": round(over_credits, 2),
                "wasted_cost_usd": round(over_credits * CREDIT_PRICE, 2),
                "recommendation": f"Downsize from {size} — avg util <20%",
                "size": size,
            })
    compute_df = pd.DataFrame(rows)

    # ── Storage Hidden Waste ────────────────────────────────────────────────
    rows = []
    stale = ["RAW.PUBLIC.OLD_IMPORT_2025", "STAGING.TEMP.MIGRATION_BACKUP",
             "ANALYTICS.ARCHIVE.LEGACY_REPORTS", "ML_FEATURES.OLD.V1_FEATURES",
             "RAW.PUBLIC.ABANDONED_POC_DATA", "STAGING.TEMP.ETL_DEBUG_COPY"]
    for table in stale:
        tb = np.random.uniform(5, 40) * _COST_SCALE
        # Actual cost includes TT + failsafe overhead (~30% extra)
        actual_monthly = tb * 23 * 1.3
        rows.append({
            "object_name": table, "waste_type": "STALE_TABLE",
            "size_gb": round(tb * 1024, 1),
            "days_since_access": int(np.random.uniform(90, 365)),
            "actual_cost_usd": round(actual_monthly, 2),
            "monthly_cost_usd": round(tb * 23, 2),
            "recommendation": "Archive or drop — no access in 90+ days",
        })
    for table in ["ANALYTICS.CORE.FACT_ORDERS", "RAW.PUBLIC.RAW_CLICKSTREAM"]:
        tb = np.random.uniform(10, 30) * _COST_SCALE
        # Actual = full table cost; saving = just the TT excess portion (~80% of TT)
        actual_monthly = tb * 23 * 1.5  # full table with TT
        saving_monthly = tb * 23 * 0.8  # recoverable TT portion
        rows.append({
            "object_name": table, "waste_type": "TIME_TRAVEL_EXCESS",
            "size_gb": round(tb * 1024, 1), "days_since_access": 0,
            "actual_cost_usd": round(actual_monthly, 2),
            "monthly_cost_usd": round(saving_monthly, 2),
            "recommendation": "Reduce retention from 90 to 7 days",
        })
    for i in range(3):
        tb = np.random.uniform(8, 25) * _COST_SCALE
        actual_monthly = tb * 23
        rows.append({
            "object_name": f"STAGING.CLONES.DEV_CLONE_{i+1}",
            "waste_type": "ABANDONED_CLONE",
            "size_gb": round(tb * 1024, 1),
            "days_since_access": int(np.random.uniform(30, 180)),
            "actual_cost_usd": round(actual_monthly, 2),
            "monthly_cost_usd": round(actual_monthly, 2),
            "recommendation": "Drop abandoned clone",
        })
    storage_df = pd.DataFrame(rows)

    # ── AI/Cortex Shadow Waste (6 patterns from snowflake-ai-finops) ──────
    # Credit amounts are scaled from the prior $4M demo so $ waste tracks TCO.
    def _ai_cr(credits: float) -> float:
        return credits * _COST_SCALE

    rows = []
    # P1: Over-sized models
    for func, model, task, calls, cr, alt, sav in [
        ("AI_COMPLETE", "claude-3-5-sonnet", "sentiment", 850, _ai_cr(4680), "AI_SENTIMENT", "50-70%"),
        ("AI_COMPLETE", "mistral-large2", "classification", 620, _ai_cr(2280), "AI_CLASSIFY", "50-75%"),
        ("AI_COMPLETE", "llama3.1-70b", "extraction", 430, _ai_cr(1580), "AI_EXTRACT", "30-60%"),
    ]:
        rows.append({
            "waste_pattern": "OVERSIZED_MODEL", "function_name": func,
            "model_name": model, "task_type": task, "calls_30d": calls,
            "actual_cost_usd": round(cr * CREDIT_PRICE, 2),
            "wasted_credits": round(cr * 0.6, 2),
            "wasted_cost_usd": round(cr * 0.6 * CREDIT_PRICE, 2),
            "recommendation": f"Replace with {alt} — est. {sav} savings",
        })
    # P2: Duplicate calls
    for func, cause, calls, cr in [
        ("AI_SENTIMENT", "hourly_no_incremental", 12000, _ai_cr(180)),
        ("AI_COMPLETE", "notebook_rerun", 3500, _ai_cr(420)),
        ("AI_CLASSIFY", "retry_on_success", 2800, _ai_cr(95)),
    ]:
        rows.append({
            "waste_pattern": "DUPLICATE_CALLS", "function_name": func,
            "model_name": cause, "task_type": "duplicate",
            "calls_30d": calls,
            "actual_cost_usd": cr * CREDIT_PRICE * 1.8,
            "wasted_credits": float(cr),
            "wasted_cost_usd": cr * CREDIT_PRICE,
            "recommendation": "Incremental processing / cache results",
        })
    # P3: Verbose prompts
    rows.append({
        "waste_pattern": "VERBOSE_PROMPTS", "function_name": "AI_COMPLETE",
        "model_name": "llama3.1-70b", "task_type": "prompt_bloat",
        "calls_30d": 15000,
        "actual_cost_usd": _ai_cr(320.0) * CREDIT_PRICE * 3.0,
        "wasted_credits": _ai_cr(320.0),
        "wasted_cost_usd": _ai_cr(320.0) * CREDIT_PRICE,
        "recommendation": "Trim prompts — avg 200 tokens filler/call",
    })
    # P4: Idle Cortex Search
    for svc, cr in [("ABANDONED_POC_SEARCH", _ai_cr(45)), ("OLD_DEMO_SEARCH", _ai_cr(28))]:
        rows.append({
            "waste_pattern": "IDLE_SEARCH_SERVICE",
            "function_name": "CORTEX_SEARCH", "model_name": svc,
            "task_type": "idle_indexing", "calls_30d": 0,
            "actual_cost_usd": cr * CREDIT_PRICE,
            "wasted_credits": float(cr),
            "wasted_cost_usd": cr * CREDIT_PRICE,
            "recommendation": "Drop idle search service — 0 queries/30d",
        })
    # P5: Agent loops
    rows.append({
        "waste_pattern": "AGENT_LOOP", "function_name": "CORTEX_AGENT",
        "model_name": "support_agent_v2", "task_type": "unbounded_loop",
        "calls_30d": 85,
        "actual_cost_usd": _ai_cr(250.0) * CREDIT_PRICE * 2.0,
        "wasted_credits": _ai_cr(250.0),
        "wasted_cost_usd": _ai_cr(250.0) * CREDIT_PRICE,
        "recommendation": "Set token budget (50K) and time limit (120s)",
    })
    # P6: Dev in prod
    rows.append({
        "waste_pattern": "DEV_IN_PROD", "function_name": "AI_COMPLETE",
        "model_name": "claude-3-5-sonnet", "task_type": "dev_experiment",
        "calls_30d": 2200,
        "actual_cost_usd": _ai_cr(380.0) * CREDIT_PRICE,
        "wasted_credits": _ai_cr(380.0),
        "wasted_cost_usd": _ai_cr(380.0) * CREDIT_PRICE,
        "recommendation": "Revoke AI access from DEV_ROLE in prod",
    })
    ai_df = pd.DataFrame(rows)

    compute_df, storage_df, ai_df = _scale_hidden_waste(compute_df, storage_df, ai_df)

    pq.write_table(pa.Table.from_pandas(compute_df), OUTPUT_DIR / "hidden_waste_compute.parquet")
    _log_written(
        "hidden_waste_compute",
        len(compute_df),
        wasted_cost_usd=int(round(float(compute_df["wasted_cost_usd"].sum()))),
    )
    pq.write_table(pa.Table.from_pandas(storage_df), OUTPUT_DIR / "hidden_waste_storage.parquet")
    _log_written(
        "hidden_waste_storage",
        len(storage_df),
        monthly_cost_usd=int(round(float(storage_df["monthly_cost_usd"].sum()))),
    )
    pq.write_table(pa.Table.from_pandas(ai_df), OUTPUT_DIR / "hidden_waste_ai.parquet")
    _log_written(
        "hidden_waste_ai",
        len(ai_df),
        wasted_cost_usd=int(round(float(ai_df["wasted_cost_usd"].sum()))),
        waste_pct_target=HIDDEN_WASTE_PCT_TARGET,
    )


def main() -> None:
    logger.info(
        "snowflake_synthetic_started",
        account=ACCOUNT,
        start=str(START_DATE),
        end=str(START_DATE + timedelta(days=DAYS - 1)),
        monthly_credits=round(float(MONTHLY_CREDITS), 1),
        annual_cost_target=ANNUAL_COST_TARGET,
        monthly_cost_cap=MONTHLY_COST_CAP,
        annual_cost_cap=ANNUAL_COST_CAP,
    )

    generate_warehouse_metering_history()
    generate_warehouse_load_history()
    query_df = generate_query_history()
    generate_query_attribution_history(query_df)
    generate_storage_usage()
    generate_table_storage_metrics()
    generate_tag_references()
    generate_warehouse_events_history()
    generate_metering_history()
    generate_cortex_ai_functions_usage()
    generate_cortex_search_daily_usage()
    generate_metering_daily_history()
    generate_serverless_services_data()
    generate_shadow_waste_data()

    files = len(list(OUTPUT_DIR.glob("*.parquet")))
    logger.info("snowflake_synthetic_built", files=files, path=str(OUTPUT_DIR))


if __name__ == "__main__":
    main()
