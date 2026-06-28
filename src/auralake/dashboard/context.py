"""Shared dashboard session context — global date range and sidebar chrome."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import streamlit as st

from auralake.dashboard.data import gold_df
from auralake.dashboard.data import to_date as _d
from auralake.dashboard.theme import date_range
from auralake.transform.catalog import discover_provider_groups


def global_charge_bounds() -> tuple[date, date] | None:
    """Min/max charge_day across all provider groups (None if no trend data)."""
    lo: date | None = None
    hi: date | None = None
    for group in discover_provider_groups():
        bounds = gold_df(
            f'SELECT min(charge_day) AS lo, max(charge_day) AS hi FROM "{group}".spend_trend_daily'
        )
        if bounds.empty or pd.isna(bounds["lo"].iloc[0]):
            continue
        g_lo, g_hi = _d(bounds["lo"].iloc[0]), _d(bounds["hi"].iloc[0])
        lo = g_lo if lo is None else min(lo, g_lo)
        hi = g_hi if hi is None else max(hi, g_hi)
    if lo is None or hi is None:
        return None
    return lo, hi


def default_range_start(lo: date, hi: date) -> date:
    """First month with material spend — avoids anchoring on a one-day sliver at ``lo``."""
    candidates: list[date] = []
    for group in discover_provider_groups():
        first = gold_df(
            f'SELECT min(charge_month) AS m FROM "{group}".monthly_bill WHERE abs(net_cost) >= 1'
        )
        if not first.empty and pd.notna(first["m"].iloc[0]):
            candidates.append(_d(first["m"].iloc[0]))
    return min(candidates) if candidates else lo


def init_global_range() -> tuple[date, date] | None:
    """Sidebar date picker shared by all pages; stores ``aura_start`` / ``aura_end``."""
    bounds = global_charge_bounds()
    if bounds is None:
        return None
    lo, hi = bounds
    default_lo = default_range_start(lo, hi)
    start, end = date_range(lo, hi, key="global_range", default_lo=default_lo)
    st.session_state["aura_start"] = start
    st.session_state["aura_end"] = end
    return start, end


def global_range() -> tuple[date, date]:
    """Current global range from session (falls back to full bounds)."""
    bounds = global_charge_bounds()
    if bounds is None:
        today = datetime.now(tz=UTC).date()
        return today, today
    lo, hi = bounds
    start = st.session_state.get("aura_start", lo)
    end = st.session_state.get("aura_end", hi)
    if not isinstance(start, date):
        start = lo
    if not isinstance(end, date):
        end = hi
    return start, end


def range_has_partial_month(end: date) -> bool:
    """True when the selected end date falls in the still-accruing current month."""
    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    return end.replace(day=1) >= current


def tco_charge_month(end: date) -> str:
    """Month key (first of month) for TCO views, aligned to the global range end."""
    return end.replace(day=1).isoformat()
