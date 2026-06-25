"""Dashboard data access — GOLD Parquet → pandas, cached on the GOLD file set.

Queries run through a throwaway in-memory DuckDB with ``<group>.<view>`` registered
(:func:`auralake.lake.duck.register_gold`). Results are cached by Streamlit keyed
on the SQL plus a signature of the GOLD files' mtimes, so a new ``auralake
ingest`` publish transparently invalidates the cache.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from auralake.lake import duck, paths


def _signature() -> tuple[tuple[str, int], ...]:
    # Keyed on the path relative to gold/ so two groups' same-named files don't collide.
    gold = paths.gold_dir()
    return tuple(
        sorted(
            (p.relative_to(gold).as_posix(), p.stat().st_mtime_ns)
            for p in gold.glob("*/*.parquet")
        )
    )


def has_data() -> bool:
    """True once at least one GOLD view has been published."""
    return any(paths.gold_dir().glob("*/*.parquet"))


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
    return _query(sql, _signature())
