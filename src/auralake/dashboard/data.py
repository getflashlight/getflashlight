"""Dashboard data access — GOLD Parquet → pandas, cached on the GOLD file set.

Queries run through a throwaway in-memory DuckDB with ``<group>.<view>`` registered
(:func:`auralake.lake.duck.register_gold`). Results are cached by Streamlit keyed
on the SQL plus a signature of the GOLD files' mtimes, so a new ``auralake
ingest`` publish transparently invalidates the cache.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import streamlit as st

from auralake.lake import duck, paths


def to_date(value: object) -> date:
    """Coerce a DuckDB/pandas date-ish scalar to a plain ``date``."""
    ts = pd.Timestamp(value)
    return date(ts.year, ts.month, ts.day)


NO_DATA_MSG = (
    "No billing data yet. Ask your admin to connect your cloud accounts in Auralake."
)


def has_data() -> bool:
    """True once at least one GOLD view has been published."""
    return any(paths.gold_dir().glob("*/*.parquet"))


def provider_label(group: str) -> str:
    """Human label for a provider group — the provider_name in its data, else the titled slug."""
    try:
        df = gold_df(f'SELECT provider_name FROM "{group}".monthly_bill LIMIT 1')
        if not df.empty and df["provider_name"].iloc[0]:
            return str(df["provider_name"].iloc[0])
    except Exception:  # noqa: BLE001 - fall back to a readable slug on any query issue
        pass
    return group.replace("_", " ").title()


@st.cache_data(show_spinner=False)
def _query(sql: str, signature: tuple[tuple[str, int], ...]) -> pd.DataFrame:
    con = duck.connect()
    try:
        duck.register_gold(con)
        return con.execute(sql).df()
    finally:
        con.close()


def gold_df(sql: str) -> pd.DataFrame:
    """Run *sql* over the GOLD views, returning a cached DataFrame."""
    return _query(sql, paths.gold_signature())


def gold_last_updated() -> datetime | None:
    """Latest GOLD parquet mtime — proxy for when billing data was last published."""
    gold = paths.gold_dir()
    files = list(gold.glob("*/*.parquet"))
    if not files:
        return None
    ts = max(p.stat().st_mtime for p in files)
    return datetime.fromtimestamp(ts, tz=UTC)


def attribution_coverage(month: date) -> tuple[float, float] | None:
    """TCO total and unattributed AWS for *month*, or None if no TCO row."""
    row = gold_df(
        f"SELECT total_cost, unattributed_infra_cost FROM shared.tco_summary_month "
        f"WHERE charge_month = '{month}' LIMIT 1"
    )
    if row.empty:
        return None
    total = float(row.iloc[0]["total_cost"])
    unattr = float(row.iloc[0]["unattributed_infra_cost"])
    return total, unattr
