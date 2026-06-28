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

    section_caption(f"Showing **{month_label}** — the latest month with telemetry.")
    kpi_cards(
        [
            ("Total recoverable", compact_money(waste_total + opp_total), month_label, "default"),
            ("Waste (tune it)", compact_money(waste_total), "idle · underused", "unattributed"),
            ("Opportunity (move)", compact_money(opp_total), "→ jobs compute", "default"),
            ("High confidence", compact_money(high_total), "vs candidate", "default"),
        ],
        key="waste",
    )

    with panel(tone="default"):
        section_title("Recoverable spend by entity", flush=True)
        section_caption(
            "Ranked by recoverable $. Filter by category (the cause), owner, or provider. "
            "underutilized is never shown for shared compute (no per-entity utilization)."
        )
        table = month_rows[
            [
                "entity_name",
                "entity_type",
                "provider_name",
                "owner_user",
                "owner_project",
                "waste_category",
                "lens",
                "confidence",
                "billed_cost",
                "recoverable_cost",
            ]
        ]
        filterable_table(
            table,
            filter_col="waste_category",
            file_name="waste_record.csv",
            key="waste_leaderboard",
            money_cols=["billed_cost", "recoverable_cost"],
            rename={
                "entity_name": "Entity",
                "entity_type": "Type",
                "provider_name": "Provider",
                "owner_user": "Owner",
                "owner_project": "Project",
                "waste_category": "Cause",
                "lens": "Lens",
                "confidence": "Confidence",
                "billed_cost": "Billed",
                "recoverable_cost": "Recoverable",
            },
        )
