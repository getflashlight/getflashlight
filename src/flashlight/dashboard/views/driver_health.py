"""Client driver health — a fleet-health/compliance leaderboard, not waste.

Reads the one GOLD passthrough view (``driver_health.driver_health``): query volume
per (client_driver, client_application, executed_by, month). No dollar figure and no
automated "stale version" verdict — there's no reference table of current driver
versions in this data, so humans read the leaderboard and judge.
"""

from __future__ import annotations

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df

_EMPTY_MSG = (
    "No driver-health data yet. This view needs the Databricks system-table pull "
    "(system.query.history) — run `flashlight ingest` with a Databricks connector configured."
)

_COLS = ["client_driver", "client_application", "executed_by", "provider_name", "query_count"]
_RENAME = {
    "client_driver": "Driver",
    "client_application": "Application",
    "executed_by": "User",
    "provider_name": "Provider",
    "query_count": "Queries",
}


def _df(sql: str) -> pd.DataFrame:
    """Query the driver-health view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def render() -> None:
    chrome.section_title("Client driver health")
    chrome.section_caption(
        "Which JDBC/ODBC driver versions and applications are hitting the warehouse, "
        "and who's running them. Not a waste signal — no dollar figure."
    )

    records = _df(
        "SELECT * FROM driver_health.driver_health ORDER BY charge_month DESC, query_count DESC"
    )
    if records.empty:
        ui.label(_EMPTY_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    months = sorted(records["charge_month"].astype(str).unique())
    month = months[-1]
    month_rows = records[records["charge_month"].astype(str) == month]
    month_label = pd.Timestamp(month).strftime("%b %Y")

    chrome.section_caption(f"Showing {month_label} — the latest month with telemetry.")
    with chrome.panel():
        chrome.searchable_table(
            month_rows[_COLS],
            key="driver_health",
            search_col="client_driver",
            int_cols=["query_count"],
            rename=_RENAME,
        )
