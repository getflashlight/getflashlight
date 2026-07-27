"""Efficiency / waste — recoverable spend across the platform.

Reads the one standardized GOLD view (``efficiency.waste_record`` + its KPI rollup),
rendered as a single faceted leaderboard. The category IS the cause; recoverable_cost
is the headline. WASTE = tune/right-size it; OPPORTUNITY = move it.
"""

from __future__ import annotations

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df
from flashlight.dashboard.theme import compact_money
from flashlight.efficiency.waste_rules import WASTE_RULES

_EMPTY_MSG = (
    "No efficiency data yet. This view needs the Databricks system-table pull "
    "(utilization, job runs) — run `flashlight ingest` with a Databricks connector configured."
)


def _df(sql: str) -> pd.DataFrame:
    """Query a waste view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def render(provider_name: str | None = None) -> None:
    chrome.section_title("Efficiency & waste")
    chrome.section_caption(
        "Recoverable spend — what you're billed for but don't use. Distinct from TCO: "
        "this is the inefficiency slice, not the total bill."
    )

    # No recoverable_cost floor here — waste_record only ever contains rows a rule
    # actually fired for (each branch is `WHERE {where_sql}`), so a $0 row is a real,
    # confirmed finding this provider can't honestly price, not a "nothing found" row.
    # _lens_table keeps those visible (sunk to the bottom) instead of dropping them.
    # provider_name is always a hardcoded caller literal (e.g. "Databricks"), never
    # user input — no SQL-escaping needed for a constant.
    where = f" WHERE provider_name = '{provider_name}'" if provider_name else ""
    records = _df(f"SELECT * FROM efficiency.waste_record{where} ORDER BY recoverable_cost DESC")
    if records.empty:
        ui.label(_EMPTY_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
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
    chrome.section_caption(f"Showing {month_label} — the latest month with telemetry.")
    chrome.kpi_row(
        [
            (
                "Waste (tune it)",
                compact_money(waste_total),
                "idle · underused",
                "unattributed",
            ),
            ("Opportunity (move)", compact_money(opp_total), "→ jobs compute", "decrease"),
            ("High confidence", compact_money(high_total), "of waste"),
            ("Entities flagged", f"{n_entities:,}", month_label),
        ],
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
    "remedy",
]
_CONSTANT_CANDIDATES = [
    "entity_type",
    "provider_name",
    "waste_category",
    "confidence",
    "detail",
    "remedy",
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
    "remedy": "How to recover it",
}
# waste_category -> its rule's actionable fix text — waste_record (SQL) has no room for
# free text this long, so it's joined in here from the one place it's authored.
_REMEDY_BY_CATEGORY = {r.category: r.remedy for r in WASTE_RULES}
_MIN_RECOVERABLE = 1.0  # sub-dollar rows are noise (and round to "$0" in the table)
_MAX_ROWS = 40


def _lens_table(month_rows: pd.DataFrame, lens: str, title: str, key: str) -> None:
    # Impact-first: rank by recoverable $. Confidence is a column + its own KPI; making it
    # the primary sort buried the big-$ candidate rows under a long tail of tiny high-conf
    # ones. Sub-dollar-but-positive rows are dropped as noise, but an exact-$0 row is kept
    # — that's a rule that fired with no honest $ tie (e.g. Redshift doesn't bill per-table
    # or per-query), a real finding, not nothing found. Sorting by recoverable_cost already
    # sinks those to the bottom instead of losing them.
    cost = month_rows["recoverable_cost"]
    rows = month_rows[
        (month_rows["lens"] == lens) & ((cost >= _MIN_RECOVERABLE) | (cost == 0))
    ].sort_values("recoverable_cost", ascending=False)
    if rows.empty:
        return
    rows = rows.assign(remedy=rows["waste_category"].map(_REMEDY_BY_CATEGORY))
    any_unpriced = bool((rows["recoverable_cost"] == 0).any())

    # Collapse columns that are constant across this table into one context line.
    constants = [
        c for c in _CONSTANT_CANDIDATES if c in rows and rows[c].nunique(dropna=False) == 1
    ]
    context = " · ".join(str(rows[c].iloc[0]) for c in constants)
    cols = [c for c in _COLS if c not in constants]

    with chrome.panel():
        chrome.panel_title(title)
        chrome.section_caption(
            f"Ranked by recoverable $. {('All ' + context + '. ') if context else ''}"
            "underutilized is never shown for shared compute (no per-entity utilization)."
            + (
                " $0 rows are confirmed findings with no honest price tie, not nothing"
                " found — see Detail/How to recover it."
                if any_unpriced
                else ""
            )
        )
        chrome.searchable_table(
            rows[cols],
            key=f"waste_{key}",
            search_col="waste_category",
            money_cols=["billed_cost", "recoverable_cost"],
            rename=_RENAME,
            max_rows=_MAX_ROWS,
        )
