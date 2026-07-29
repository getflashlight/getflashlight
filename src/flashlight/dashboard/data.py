"""Dashboard data access — GOLD Parquet → pandas.

Queries run through a throwaway in-memory DuckDB with ``<group>.<view>`` registered
(:func:`flashlight.lake.duck.register_gold`). No caching — DuckDB-over-Parquet reads
are already fast, and a new ``flashlight ingest`` publish should be visible on the
next query with no invalidation logic to get wrong.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd

from flashlight.lake import duck, paths


def to_date(value: object) -> date:
    """Coerce a DuckDB/pandas date-ish scalar to a plain ``date``."""
    ts = pd.Timestamp(value)
    return date(ts.year, ts.month, ts.day)


NO_DATA_MSG = (
    "No billing data yet. Ask your admin to connect your cloud accounts in Flashlight."
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


def gold_df(sql: str) -> pd.DataFrame:
    """Run *sql* over the GOLD views."""
    con = duck.connect()
    try:
        duck.register_gold(con)
        return con.execute(sql).df()
    finally:
        con.close()


def telemetry_df(sql: str) -> pd.DataFrame:
    """Run *sql* over the ``telemetry.chat_turn`` view (BYOK chat usage log)."""
    con = duck.connect()
    try:
        duck.register_chat_turns(con)
        return con.execute(sql).df()
    finally:
        con.close()


def gold_last_updated() -> datetime | None:
    """Latest GOLD parquet mtime — proxy for when billing data was last published."""
    gold = paths.gold_dir()
    files = list(gold.glob("*/*.parquet"))
    if not files:
        return None
    ts = max(p.stat().st_mtime for p in files)
    return datetime.fromtimestamp(ts, tz=UTC)
