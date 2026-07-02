"""Efficiency / waste — recoverable spend across the platform.

Reads the one standardized GOLD view (``efficiency.waste_record`` + its KPI rollup),
rendered as a single faceted leaderboard. The category IS the cause; recoverable_cost
is the headline. WASTE = tune/right-size it; OPPORTUNITY = move it.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from auralake.dashboard.data import NO_DATA_MSG, gold_df, has_data
from auralake.dashboard.theme import (
    compact_money,
    filterable_table,
    kpi_cards,
    panel,
    section_caption,
    section_title,
)

_EMPTY_MSG = (
    "No efficiency data yet. This view needs the Databricks system-table pull "
    "(utilization, job runs) — run `auralake ingest` with a Databricks connector configured."
)


def _df(sql: str) -> pd.DataFrame:
    """Query a waste view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def render() -> None:
    st.title("Efficiency & waste")
    st.caption(
        "Recoverable spend — what you're billed for but don't use. Distinct from TCO: "
        "this is the inefficiency slice, not the total bill."
    )
    if not has_data():
        st.info(NO_DATA_MSG)
        return

    records = _df(
        "SELECT * FROM efficiency.waste_record WHERE recoverable_cost > 0 "
        "ORDER BY recoverable_cost DESC"
    )
    if records.empty:
        st.info(_EMPTY_MSG)
        return

    months = sorted(records["charge_month"].astype(str).unique())
    month = months[-1]
    month_rows = records[records["charge_month"].astype(str) == month]
    month_label = pd.Timestamp(month).strftime("%b %Y")

    waste_total = month_rows.loc[month_rows["lens"] == "WASTE", "recoverable_cost"].sum()
    opp_total = month_rows.loc[month_rows["lens"] == "OPPORTUNITY", "recoverable_cost"].sum()
    high_total = month_rows.loc[month_rows["confidence"] == "high", "recoverable_cost"].sum()
    n_entities = month_rows["entity_id"].nunique()

    # WASTE and OPPORTUNITY are separate lenses (a cluster can be both — different
    # remedies), so they are shown separately, never summed into one headline.
    section_caption(f"Showing **{month_label}** — the latest month with telemetry.")
    kpi_cards(
        [
            ("Waste (tune it)", compact_money(waste_total), "idle · underused", "unattributed"),
            ("Opportunity (move)", compact_money(opp_total), "→ jobs compute", "default"),
            ("High confidence", compact_money(high_total), "of waste", "default"),
            ("Entities flagged", f"{n_entities:,}", month_label, "default"),
        ],
        key="waste",
    )

    # Split by lens — WASTE and OPPORTUNITY are different remedies and share billed_cost
    # for the same entity, so a single flat list invites double-counting.
    _lens_table(month_rows, "WASTE", "Waste — tune or right-size it", "waste")
    _lens_table(month_rows, "OPPORTUNITY", "Opportunity — move it to cheaper compute", "opp")


# Displayed in order; descriptor columns that are constant across a lens table collapse
# into the caption instead of repeating on every row (e.g. placement is always
# interactive · candidate · → jobs compute).
_COLS = [
    "entity_name",
    "entity_type",
    "provider_name",
    "owner_user",
    "waste_category",
    "confidence",
    "detail",
    "billed_cost",
    "recoverable_cost",
]
_CONSTANT_CANDIDATES = [
    "entity_type",
    "provider_name",
    "waste_category",
    "confidence",
    "detail",
]
_RENAME = {
    "entity_name": "Entity",
    "entity_type": "Type",
    "provider_name": "Provider",
    "owner_user": "Owner",
    "waste_category": "Cause",
    "confidence": "Confidence",
    "detail": "Detail",
    "billed_cost": "Billed",
    "recoverable_cost": "Recoverable",
}
_MIN_RECOVERABLE = 1.0  # sub-dollar rows are noise (and round to "$0" in the table)
_MAX_ROWS = 40


def _lens_table(month_rows: pd.DataFrame, lens: str, title: str, key: str) -> None:
    # Impact-first: rank by recoverable $. Confidence is a column + its own KPI; making it
    # the primary sort buried the big-$ candidate rows under a long tail of tiny high-conf
    # ones. Drop sub-dollar rows — they only add noise and display as "$0".
    rows = month_rows[
        (month_rows["lens"] == lens) & (month_rows["recoverable_cost"] >= _MIN_RECOVERABLE)
    ].sort_values("recoverable_cost", ascending=False)
    if rows.empty:
        return

    # Collapse columns that are constant across this table into one context line.
    constants = [
        c for c in _CONSTANT_CANDIDATES if c in rows and rows[c].nunique(dropna=False) == 1
    ]
    context = " · ".join(str(rows[c].iloc[0]) for c in constants)
    cols = [c for c in _COLS if c not in constants]

    with panel(tone="default"):
        section_title(title, flush=True)
        section_caption(
            f"Ranked by recoverable $. {('All ' + context + '. ') if context else ''}"
            "underutilized is never shown for shared compute (no per-entity utilization)."
        )
        filterable_table(
            rows[cols],
            filter_col="waste_category",
            file_name=f"waste_{key}.csv",
            key=f"waste_{key}",
            money_cols=["billed_cost", "recoverable_cost"],
            rename=_RENAME,
            max_rows=_MAX_ROWS,
        )
