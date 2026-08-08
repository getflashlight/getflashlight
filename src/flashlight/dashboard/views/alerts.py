"""Alerts — MoM spend changes that used to sit as a header caption under the KPIs.

The long "rose $X… mostly SKU" line felt out of place between the KPI row and the
tabs. It lives here as a dedicated tab on every provider page: window net, MoM
delta vs the latest complete month, and the top movers at that provider's natural
grain (SKU for Databricks, service otherwise). Breakdown still has the deeper
month-over-month table.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.summary import compute_provider_spend_alert
from flashlight.dashboard.theme import compact_money


def render(
    group: str,
    label: str,
    start: date,
    end: date,
    *,
    partial: bool,
    bill_view: str = "monthly_bill",
    scope_sql: str = "",
) -> None:
    chrome.section_title("Alerts")
    chrome.section_caption(
        f"Notable month-over-month changes in {label} net spend. "
        "Compares the latest complete month — never a still-accruing current month."
    )

    alert = compute_provider_spend_alert(
        group,
        label,
        start,
        end,
        partial=partial,
        bill_view=bill_view,
        scope_sql=scope_sql,
    )

    if not alert.window_net:
        ui.label(f"No {label} spend in the selected range.").classes("text-sm").style(
            f"color:{chrome.INK_MUTED}"
        )
        return

    partial_note = " · current month is still accruing" if alert.partial else ""
    with chrome.panel():
        chrome.panel_title("Selected window")
        ui.label(
            f"{compact_money(alert.window_net)} net{partial_note}"
        ).classes("text-sm").style(f"color:{chrome.INK_SECONDARY}")

        if alert.prior_month is None or not alert.prior_net:
            ui.label(
                "Not enough complete months in range to compare month-over-month."
            ).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
            return

        assert alert.cmp_month is not None
        delta = alert.delta
        assert delta is not None
        pct = alert.pct_change
        assert pct is not None

        chrome.panel_title(f"{alert.cmp_month:%b %Y} vs {alert.prior_month:%b %Y}")
        if delta == 0:
            ui.label(f"Flat vs {alert.prior_month:%b %Y} at month grain.").classes(
                "text-sm"
            ).style(f"color:{chrome.INK_SECONDARY}")
        else:
            verb = "Rose" if delta > 0 else "Fell"
            ui.label(
                f"{verb} {compact_money(abs(delta))} ({pct:+.1f}%) "
                f"· {compact_money(alert.cur_net)} vs {compact_money(alert.prior_net)}"
            ).classes("text-sm").style(f"color:{chrome.INK_SECONDARY}")

        if not alert.movers:
            ui.label(f"No {alert.driver_label.lower()} movement rows for that month.").classes(
                "text-sm"
            ).style(f"color:{chrome.INK_MUTED}")
            return

        chrome.panel_title(f"Top {alert.driver_label} movers")
        rows = pd.DataFrame(
            [
                {"name": name, "cost_delta": d, "cost_pct_change": p}
                for name, d, p in alert.movers
            ]
        )
        chrome.heatmap_table(
            rows,
            heat_col="cost_pct_change",
            key=f"alerts_movers_{group}",
            money_cols=["cost_delta"],
            rename={
                "name": alert.driver_label,
                "cost_delta": "Δ vs prior",
                "cost_pct_change": "MoM %",
            },
        )
