"""Snowflake Visibility dashboard — 11 Optimization Levers.

Aligned with the Major Optimization Levers framework:
  01 Warehouse / Compute
  02 Query Performance
  03 Storage
  04 Lakehouse / Iceberg Table
  05 Data Design
  06 Ingestion and Orchestration
  07 AI and Cortex
  08 Data Movement and Availability
  09 Snowpark SPCS and Openflow
  10 Governance
  11 Change Safety

Three cost-driver pillars feed these levers:
  Managed Service | Serverless Services | AI Cost Drivers
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.snowflake import visibility_data as _sf_data_default
from flashlight.dashboard.theme import compact_money, rgba_hex

# Module-level variable — swapped temporarily by render()/render_leaderboard()
# so all private functions pick up the correct data source for the current page render.
sf_data = _sf_data_default

# Shared color map for service category charts — applied to both the current-month
# pie and the 12-month stacked bar so colors always match.
_SERVICE_CATEGORY_COLORS: dict[str, str] = {
    "Managed Compute":    "#3987e5",  # blue  (chrome.ACCENT)
    "Serverless Compute": "#199e70",  # green (chrome.OPPORTUNITY)
    "AI & ML":            "#9085e9",  # purple
    "Storage":            "#f4a261",  # amber
    "Data Transfer":      "#c98500",  # dark amber
    "Other":              "#898781",  # grey  (chrome.INK_MUTED)
}

# Same legend label as Databricks provider_focus — projection must never look measured.
_FORECAST_SERIES = "Forecast (projection)"


def _forecast_marker() -> dict[str, object]:
    """Hatched cool-slate marker for projected bars (Databricks monthly forecast)."""
    return dict(
        color=rgba_hex(chrome.SEMANTIC["forecast"], 0.55),
        pattern=dict(
            shape="/",
            fgcolor=chrome.SEMANTIC["forecast"],
            bgcolor=rgba_hex(chrome.SEMANTIC["forecast"], 0.15),
            size=7,
            solidity=0.35,
        ),
    )


def _service_category_order(bills: pd.DataFrame) -> list[str]:
    """Largest category first; keep Cost Distribution color-key order as a tiebreak."""
    if bills.empty or "service_name" not in bills.columns:
        return list(_SERVICE_CATEGORY_COLORS)
    totals = bills.groupby("service_name")["net_cost"].sum().sort_values(ascending=False)
    known = [c for c in _SERVICE_CATEGORY_COLORS if c in totals.index]
    rest = [c for c in totals.index if c not in known]
    return known + rest


def render_leaderboard(data: Any = None) -> None:
    """Public entry point for LeaderBoard — called from router."""
    global sf_data
    sf_data = data if data is not None else _sf_data_default
    try:
        chrome.section_title("LeaderBoard")
        _leaderboard()
    finally:
        sf_data = _sf_data_default


def render(data: Any = None) -> None:
    """Main entry point for Visibility — called by router."""
    global sf_data
    sf_data = data if data is not None else _sf_data_default
    try:
        chrome.section_title("Visibility")

        with ui.tabs().classes("w-full").props("dense") as tabs:
            tab_overview = ui.tab("Overview")
            tab_wh = ui.tab("Warehouse/Compute")
            tab_serverless = ui.tab("Serverless Compute")
            tab_ai = ui.tab("AI & Cortex")
            tab_storage = ui.tab("Storage")
            tab_query = ui.tab("Query Performance")
            tab_heatmaps = ui.tab("Heatmaps")
            tab_design = ui.tab("Data Design")
            tab_ingest = ui.tab("Ingestion & Orch.")
            tab_movement = ui.tab("Data Movement")
            tab_iceberg = ui.tab("Lakehouse/Iceberg")
            tab_spcs = ui.tab("SPCS & Openflow")

        with ui.tab_panels(
            tabs, value=tab_overview
        ).classes("w-full").style("background:transparent;"):
            with ui.tab_panel(tab_overview):
                _overview(data)
            with ui.tab_panel(tab_wh):
                _warehouse_compute(data)
            with ui.tab_panel(tab_serverless):
                _serverless_compute()
            with ui.tab_panel(tab_ai):
                _ai_cortex()
            with ui.tab_panel(tab_storage):
                _storage()
            with ui.tab_panel(tab_query):
                _query_performance()
            with ui.tab_panel(tab_heatmaps):
                _heatmaps(data)
            with ui.tab_panel(tab_design):
                _data_design()
            with ui.tab_panel(tab_ingest):
                _ingestion_orchestration()
            with ui.tab_panel(tab_movement):
                _data_movement()
            with ui.tab_panel(tab_iceberg):
                _lakehouse_iceberg()
            with ui.tab_panel(tab_spcs):
                _spcs_openflow()
    finally:
        sf_data = _sf_data_default


# ═══════════════════════════════════════════════════════════════════════════════
# LeaderBoard (3-Pillar View)
# ═══════════════════════════════════════════════════════════════════════════════

def _leaderboard() -> None:
    """Three cost-driver pillars plus shared attribution layer."""
    kpis = sf_data.kpi_summary()
    ai = sf_data.ai_spend_summary()
    ai_pct = round(ai["ai_cost"] / max(kpis["total_cost"], 1) * 100, 0)
    sw = sf_data.hidden_waste_summary()
    chrome.kpi_row([
        (f"TCO ({kpis['month_label']})", f"${kpis['total_cost']:,.0f}",
         f"{kpis['total_credits']:,.0f} credits"),
        ("Compute", f"${kpis['compute_cost'] + kpis['serverless_compute_cost']:,.0f}",
         f"Managed ${kpis['compute_cost']:,.0f} | "
         f"Serverless ${kpis['serverless_compute_cost']:,.0f}"),
        ("AI Spend", f"${ai['ai_cost']:,.0f}",
         f"{ai_pct:.0f}% of TCO", "neutral"),
        ("Storage Cost", f"${kpis['storage_cost']:,.0f}",
         f"{kpis['storage_tb']:.0f} TB"),
        ("Hidden Waste", f"${sw['total']:,.0f}",
         f"{sw['waste_pct']:.0f}% of last 30-day spend", "increase"),
        ("Year to Date Spend", f"${kpis['ytd_cost']:,.0f}",
         "Calendar year total"),
    ], columns=3)

    # ── TCO Trend & Forecast (stacked bars by service, like Databricks) ───
    forecast_df = sf_data.tco_monthly_trend_and_forecast()
    monthly = sf_data.cost_breakdown_monthly(12)
    if not forecast_df.empty or not monthly.empty:
        actual_months: set[str] = set()
        if not forecast_df.empty:
            actual_rows = forecast_df[forecast_df["type"] == "Actual"]
            if not actual_rows.empty:
                actual_months = set(
                    pd.to_datetime(actual_rows["month"]).dt.strftime("%Y-%m")
                )
        # Current partial month (e.g. Aug MTD) — show measured stack + hatched remainder.
        partial_month: str | None = None
        if not monthly.empty:
            latest = str(monthly["month"].max())
            if latest not in actual_months:
                partial_month = latest

        bills = pd.DataFrame()
        if not monthly.empty:
            keep = set(actual_months)
            if partial_month is not None:
                keep.add(partial_month)
            bills = monthly.copy()
            if keep:
                bills = bills[bills["month"].isin(keep)]
            bills = bills.rename(
                columns={"category": "service_name", "cost_usd": "net_cost"}
            )
            bills = bills[bills["net_cost"].abs() > 1e-9]

        with ui.row().classes("w-full gap-4"):
            with ui.column().classes("flex-[2]"):
                with chrome.panel():
                    chrome.panel_title("TCO Trend & 6-Month Forecast")
                    chrome.section_caption(
                        "Stacked monthly TCO by service — same categories as Cost "
                        "Distribution. Forecast is a projection, not measured spend."
                    )
                    fig_fc = go.Figure()
                    if not bills.empty:
                        fig_fc = px.bar(
                            bills,
                            x="month",
                            y="net_cost",
                            color="service_name",
                            color_discrete_map=_SERVICE_CATEGORY_COLORS,
                            category_orders={
                                "service_name": _service_category_order(bills)
                            },
                            barmode="stack",
                            labels={
                                "month": "",
                                "net_cost": "",
                                "service_name": "",
                            },
                        )
                    forecast = (
                        forecast_df[forecast_df["type"] == "Forecast"].copy()
                        if not forecast_df.empty
                        else pd.DataFrame()
                    )
                    remainder: tuple[str, float] | None = None
                    if not forecast.empty:
                        forecast["month_str"] = pd.to_datetime(
                            forecast["month"]
                        ).dt.strftime("%Y-%m")
                        # Partial month: actual MTD (colored stack) + projected remainder.
                        if partial_month is not None and not bills.empty:
                            mtd = float(
                                bills.loc[
                                    bills["month"] == partial_month, "net_cost"
                                ].sum()
                            )
                            fc_row = forecast[
                                forecast["month_str"] == partial_month
                            ]
                            if not fc_row.empty:
                                projected = float(fc_row["tco"].iloc[0])
                                rem = round(projected - mtd, 2)
                                if rem > 0:
                                    remainder = (partial_month, rem)
                        # Whole-month forecast bars only for months with no actual stack.
                        billed_months = (
                            set(bills["month"].astype(str)) if not bills.empty else set()
                        )
                        future = forecast[~forecast["month_str"].isin(billed_months)]
                        if not future.empty:
                            fig_fc.add_bar(
                                x=list(future["month_str"]),
                                y=[float(v) for v in future["tco"]],
                                name=_FORECAST_SERIES,
                                legendgroup="forecast",
                                # No customdata — projection stays inert on click.
                                marker=_forecast_marker(),
                                hovertemplate=(
                                    "%{x}<br>forecast $%{y:,.0f}<extra></extra>"
                                ),
                            )
                        if remainder is not None:
                            rem_month, rem_cost = remainder
                            fig_fc.add_bar(
                                x=[rem_month],
                                y=[rem_cost],
                                name=_FORECAST_SERIES,
                                legendgroup="forecast",
                                showlegend=future.empty,
                                marker=_forecast_marker(),
                                hovertemplate=(
                                    "%{x}<br>projected remainder $%{y:,.0f}"
                                    "<extra></extra>"
                                ),
                            )
                    months_on_axis: list[str] = []
                    if not bills.empty:
                        months_on_axis.extend(str(m) for m in bills["month"].unique())
                    if not forecast.empty and "month_str" in forecast.columns:
                        months_on_axis.extend(str(m) for m in forecast["month_str"])
                    if months_on_axis:
                        fig_fc.update_xaxes(
                            type="category",
                            categoryorder="array",
                            categoryarray=sorted(set(months_on_axis)),
                        )
                    # One label per column — stack height (actual and/or projection).
                    totals: dict[str, float] = {}
                    if not bills.empty:
                        totals = {
                            str(k): float(v)
                            for k, v in bills.groupby("month")["net_cost"].sum().items()
                        }
                    if not forecast.empty and "month_str" in forecast.columns:
                        billed = set(totals)
                        for m, v in zip(
                            forecast["month_str"], forecast["tco"], strict=True
                        ):
                            ms = str(m)
                            if ms in billed:
                                continue  # remainder annotation added below
                            totals[ms] = totals.get(ms, 0.0) + float(v)
                    if remainder is not None:
                        rem_month, rem_cost = remainder
                        totals[rem_month] = totals.get(rem_month, 0.0) + rem_cost
                    for month, total in totals.items():
                        fig_fc.add_annotation(
                            x=month,
                            y=total,
                            text=compact_money(total),
                            showarrow=False,
                            yshift=10,
                            font=dict(size=11, color=chrome.INK_SECONDARY),
                        )
                    chrome.plot(
                        chrome.style_fig(
                            fig_fc, height=400, has_legend=True, category_x=True
                        )
                    )

            with ui.column().classes("flex-1"):
                with chrome.panel():
                    chrome.panel_title("Monthly TCO Summary")
                    if forecast_df.empty:
                        ui.label("No monthly TCO summary yet.").classes("text-sm").style(
                            f"color:{chrome.INK_MUTED}"
                        )
                    else:
                        table_df = forecast_df[["month", "tco", "type"]].copy()
                        table_df["Month"] = table_df["month"].dt.strftime("%b %Y")
                        table_df["month_str"] = table_df["month"].dt.strftime("%Y-%m")
                        mtd_by_month: dict[str, float] = {}
                        if not monthly.empty:
                            mtd_by_month = (
                                monthly.groupby("month")["cost_usd"].sum().to_dict()
                            )

                        def _actual_cell(r: pd.Series) -> str:
                            if r["type"] == "Actual":
                                return f"${r['tco']:,.0f}"
                            # Partial month: show measured MTD beside the full-month forecast.
                            if (
                                partial_month is not None
                                and r["month_str"] == partial_month
                                and partial_month in mtd_by_month
                            ):
                                return f"${mtd_by_month[partial_month]:,.0f}"
                            return "—"

                        table_df["TCO Actual"] = table_df.apply(_actual_cell, axis=1)
                        table_df["TCO Forecast"] = table_df.apply(
                            lambda r: f"${r['tco']:,.0f}"
                            if r["type"] == "Forecast"
                            else "—",
                            axis=1,
                        )
                        columns = [
                            {
                                "name": "Month",
                                "label": "Month",
                                "field": "Month",
                                "align": "left",
                            },
                            {
                                "name": "Actual",
                                "label": "Actual",
                                "field": "TCO Actual",
                                "align": "right",
                            },
                            {
                                "name": "Forecast",
                                "label": "Forecast",
                                "field": "TCO Forecast",
                                "align": "right",
                            },
                        ]
                        rows = table_df[
                            ["Month", "TCO Actual", "TCO Forecast"]
                        ].to_dict("records")
                        ui.table(
                            columns=columns,
                            rows=rows,
                        ).props("dense flat hide-pagination").classes("w-full").style(
                            f"background:{chrome.SURFACE};"
                            f"color:{chrome.INK_PRIMARY};"
                            "font-size:12px;"
                        )
    # ── Cost Breakdown Pie Chart ───────────────────────────────────────────
    breakdown = sf_data.cost_breakdown()
    if breakdown:
        labels = [item["label"] for item in breakdown if item["cost"] > 0]
        values = [item["cost"] for item in breakdown if item["cost"] > 0]
        colors = [_SERVICE_CATEGORY_COLORS.get(lbl, "#898781") for lbl in labels]

        with chrome.panel():
            chrome.panel_title("Cost Distribution by Service")
            fig = go.Figure(go.Pie(
                labels=labels,
                values=values,
                hole=0.4,
                marker=dict(colors=colors),
                hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<br>"
                              "%{percent}<extra></extra>",
                textinfo="label+percent",
                textposition="outside",
            ))
            chrome.style_fig(fig, height=450, has_legend=False, currency_axis=None)
            fig.update_layout(margin=dict(l=20, r=20, t=40, b=20))
            chrome.plot(fig)

    # ── Monthly Cost by Service — 12-month stacked bar ────────────────────
    monthly = sf_data.cost_breakdown_monthly(12)
    if not monthly.empty:
        with chrome.panel():
            chrome.panel_title(
                "Cost by Service — Last 12 Months"
            )
            fig_bar = px.bar(
                monthly,
                x="month",
                y="cost_usd",
                color="category",
                color_discrete_map=_SERVICE_CATEGORY_COLORS,
                category_orders={"category": list(_SERVICE_CATEGORY_COLORS)},
                labels={"month": "", "cost_usd": "", "category": ""},
            )
            fig_bar.update_layout(barmode="stack")
            chrome.plot(chrome.style_fig(
                fig_bar, has_legend=True, height=320
            ))

    # ── AI & Serverless Breakdown (side by side) ──────────────────────────
    with ui.row().classes("w-full gap-4 mt-4"):
        # AI Cost Breakdown
        with ui.column().classes("flex-1"):
            with chrome.panel():
                chrome.panel_title("AI & ML Cost Breakdown")
                ai_bd = sf_data.ai_cost_breakdown()
                if ai_bd:
                    ai_labels = [item["label"] for item in ai_bd if item["cost"] > 0]
                    ai_values = [item["cost"] for item in ai_bd if item["cost"] > 0]
                    fig_ai = go.Figure(go.Pie(
                        labels=ai_labels, values=ai_values, hole=0.4,
                        hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<br>"
                                      "%{percent}<extra></extra>",
                        textinfo="label+percent", textposition="outside",
                    ))
                    chrome.style_fig(fig_ai, height=350, has_legend=False,
                                     currency_axis=None)
                    fig_ai.update_layout(margin=dict(l=10, r=10, t=20, b=10))
                    chrome.plot(fig_ai)

        # Serverless Cost Breakdown
        with ui.column().classes("flex-1"):
            with chrome.panel():
                chrome.panel_title("Serverless Compute Breakdown")
                svl_bd = sf_data.serverless_cost_breakdown()
                if svl_bd:
                    svl_labels = [item["label"] for item in svl_bd
                                  if item["cost"] > 0]
                    svl_values = [item["cost"] for item in svl_bd
                                  if item["cost"] > 0]
                    fig_svl = go.Figure(go.Pie(
                        labels=svl_labels, values=svl_values, hole=0.4,
                        hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<br>"
                                      "%{percent}<extra></extra>",
                        textinfo="label+percent", textposition="outside",
                    ))
                    chrome.style_fig(fig_svl, height=350, has_legend=False,
                                     currency_axis=None)
                    fig_svl.update_layout(margin=dict(l=10, r=10, t=20, b=10))
                    chrome.plot(fig_svl)

    # ── Account Storage Breakdown (donut) ─────────────────────────────────
    top_tables = sf_data.top_tables_storage(25)
    if not top_tables.empty:
        total_active = top_tables["active_gb"].sum()
        total_tt = top_tables["time_travel_gb"].sum()
        total_fs = top_tables["failsafe_gb"].sum()

        with chrome.panel():
            chrome.panel_title("Account Storage Breakdown")
            fig_sb = go.Figure(go.Pie(
                labels=["Active Storage", "Time Travel", "Failsafe"],
                values=[total_active, total_tt, total_fs],
                hole=0.45,
                marker_colors=[chrome.ACCENT, chrome.OPPORTUNITY, chrome.WASTE],
                hovertemplate="<b>%{label}</b><br>%{value:,.0f} GB<br>"
                              "%{percent}<extra></extra>",
                textinfo="label+percent",
                textposition="outside",
            ))
            chrome.style_fig(fig_sb, height=350, has_legend=False,
                             currency_axis=None)
            fig_sb.update_layout(margin=dict(l=20, r=20, t=20, b=20))
            chrome.plot(fig_sb)

    # ── Hidden Waste Details ──────────────────────────────────────────────
    waste_rows = []

    # Compute waste
    compute_df = sf_data.hidden_waste_compute()
    if not compute_df.empty:
        for _, row in compute_df.iterrows():
            waste_rows.append({
                "Resource": row["warehouse_name"],
                "Category": "Compute",
                "Waste Type": str(row["waste_type"]).replace("_", " ").title(),
                "Actual Cost": f"${row['actual_cost_usd']:,.0f}",
                "Potential Saving": f"${row['wasted_cost_usd']:,.0f}",
                "Flagged Reason": row["recommendation"],
                "_sort": row["wasted_cost_usd"],
            })

    # Storage waste
    storage_df = sf_data.hidden_waste_storage()
    if not storage_df.empty:
        for _, row in storage_df.iterrows():
            actual_annual = row["actual_cost_usd"] * 12
            saving_annual = row["monthly_cost_usd"] * 12
            waste_rows.append({
                "Resource": row["object_name"],
                "Category": "Storage",
                "Waste Type": str(row["waste_type"]).replace("_", " ").title(),
                "Actual Cost": f"${actual_annual:,.0f}/yr",
                "Potential Saving": f"${saving_annual:,.0f}/yr",
                "Flagged Reason": row["recommendation"],
                "_sort": saving_annual,
            })

    # AI waste
    ai_df = sf_data.hidden_waste_ai()
    if not ai_df.empty:
        for _, row in ai_df.iterrows():
            waste_rows.append({
                "Resource": f"{row['function_name']} ({row['model_name']})",
                "Category": "AI & ML",
                "Waste Type": str(row["waste_pattern"]).replace("_", " ").title(),
                "Actual Cost": f"${row['actual_cost_usd']:,.0f}",
                "Potential Saving": f"${row['wasted_cost_usd']:,.0f}",
                "Flagged Reason": row["recommendation"],
                "_sort": row["wasted_cost_usd"],
            })

    if waste_rows:
        waste_df = pd.DataFrame(waste_rows).sort_values("_sort", ascending=False)
        waste_df = waste_df.drop(columns=["_sort"])

        with chrome.panel():
            chrome.panel_title("Hidden Waste — Detail Breakdown")
            chrome.searchable_table(
                waste_df, key="lb_waste_detail",
                search_col="Resource",
                rename={
                    "Resource": "Resource",
                    "Category": "Category",
                    "Waste Type": "Waste Type",
                    "Actual Cost": "Actual Cost",
                    "Potential Saving": "Potential Saving",
                    "Flagged Reason": "Flagged Reason",
                },
            )

    # ── Hidden Waste by Category (pie) ────────────────────────────────────
    _compute = sf_data.hidden_waste_compute()
    _storage = sf_data.hidden_waste_storage()
    _ai = sf_data.hidden_waste_ai()

    waste_slices = []
    # Compute breakdown
    if not _compute.empty:
        for wtype, grp in _compute.groupby("waste_type"):
            label = f"Compute — {str(wtype).replace('_', ' ').title()}"
            waste_slices.append({"label": label, "cost": grp["wasted_cost_usd"].sum()})
    # Storage breakdown
    if not _storage.empty:
        for wtype, grp in _storage.groupby("waste_type"):
            label = f"Storage — {str(wtype).replace('_', ' ').title()}"
            waste_slices.append({"label": label, "cost": grp["monthly_cost_usd"].sum() * 12})
    # AI breakdown
    if not _ai.empty:
        for pattern, grp in _ai.groupby("waste_pattern"):
            label = f"AI — {str(pattern).replace('_', ' ').title()}"
            waste_slices.append({"label": label, "cost": grp["wasted_cost_usd"].sum()})

    waste_slices = [w for w in waste_slices if w["cost"] > 0]
    waste_slices.sort(key=lambda x: x["cost"], reverse=True)

    if waste_slices:
        with chrome.panel():
            chrome.panel_title("Hidden Waste — Where to Start")
            fig_hw = go.Figure(go.Pie(
                labels=[w["label"] for w in waste_slices],
                values=[w["cost"] for w in waste_slices],
                hole=0.4,
                hovertemplate="<b>%{label}</b><br>$%{value:,.0f}<br>"
                              "%{percent}<extra></extra>",
                textinfo="label+percent",
                textposition="outside",
            ))
            chrome.style_fig(fig_hw, height=400, has_legend=False,
                             currency_axis=None)
            fig_hw.update_layout(margin=dict(l=30, r=30, t=20, b=20))
            chrome.plot(fig_hw)

    # ── Top 5 Users Contributing to Hidden Waste ──────────────────────────
    user_waste = sf_data.top_users_hidden_waste(5)
    if not user_waste.empty:
        with chrome.panel():
            chrome.panel_title("Top 5 Users Contributing to Hidden Waste")
            display = user_waste.copy()
            display["TCO by User"] = display["tco_by_user"].apply(
                lambda x: f"${x:,.0f}")
            display["Attributed Waste"] = display["attributed_waste"].apply(
                lambda x: f"${x:,.0f}")
            display = display.rename(columns={
                "user_name": "User",
                "service_type": "Service Type",
                "comment": "Comment",
            })[["User", "TCO by User", "Attributed Waste", "Service Type", "Comment"]]

            columns = [
                {"name": "User", "label": "User",
                 "field": "User", "align": "left"},
                {"name": "TCO by User", "label": "TCO by User",
                 "field": "TCO by User", "align": "right"},
                {"name": "Attributed Waste", "label": "Attributed Waste",
                 "field": "Attributed Waste", "align": "right"},
                {"name": "Service Type", "label": "Service Type",
                 "field": "Service Type", "align": "center"},
                {"name": "Comment", "label": "Comment",
                 "field": "Comment", "align": "left"},
            ]
            rows = display.to_dict("records")
            ui.table(
                columns=columns, rows=rows,
            ).props("dense flat hide-pagination").classes("w-full").style(
                f"background:{chrome.SURFACE};color:{chrome.INK_PRIMARY};"
                "font-size:13px;")


# ═══════════════════════════════════════════════════════════════════════════════
# Overview
# ═══════════════════════════════════════════════════════════════════════════════

def _overview(data: Any = None) -> None:
    _d = data if data is not None else sf_data
    kpis = _d.kpi_summary()
    ai = _d.ai_spend_summary()
    ai_pct = round(ai["ai_cost"] / max(kpis["total_cost"], 1) * 100, 0)
    sw = _d.hidden_waste_summary()
    chrome.kpi_row([
        (f"TCO ({kpis['month_label']})", f"${kpis['total_cost']:,.0f}",
         f"{kpis['total_credits']:,.0f} credits"),
        ("Compute", f"${kpis['compute_cost'] + kpis['serverless_compute_cost']:,.0f}",
         f"Managed ${kpis['compute_cost']:,.0f} | "
         f"Serverless ${kpis['serverless_compute_cost']:,.0f}"),
        ("AI Spend", f"${ai['ai_cost']:,.0f}",
         f"{ai_pct:.0f}% of TCO", "neutral"),
        ("Storage Cost", f"${kpis['storage_cost']:,.0f}",
         f"{kpis['storage_tb']:.0f} TB"),
        ("Hidden Waste", f"${sw['total']:,.0f}",
         f"{sw['waste_pct']:.0f}% of last 30-day spend", "increase"),
        ("Year to Date Spend", f"${kpis['ytd_cost']:,.0f}",
         "Calendar year total"),
    ], columns=3)

    # Daily trend
    with chrome.panel():
        chrome.panel_title("Daily Spend Trend")

        from datetime import date as _date
        trend_state = {
            "start": _date(2026, 1, 1).isoformat(),
            "end": _date.today().isoformat(),
        }

        with ui.row().classes("items-end gap-4 w-full mb-3"):
            start_input = (
                ui.input("Start date", value=trend_state["start"])
                .props("dense outlined type=date")
                .classes("w-40")
                .style(f"color:{chrome.INK_PRIMARY}")
            )
            end_input = (
                ui.input("End date", value=trend_state["end"])
                .props("dense outlined type=date")
                .classes("w-40")
                .style(f"color:{chrome.INK_PRIMARY}")
            )
            apply_btn = ui.button("Apply", icon="refresh").props(
                "dense no-caps"
            ).style(f"background:{chrome.ACCENT};color:#fff;")

        @ui.refreshable
        def _daily_chart() -> None:
            daily = _d.spend_by_day(
                start_input.value, end_input.value)
            if daily.empty:
                ui.label("No data for selected range").style(
                    f"color:{chrome.INK_MUTED}")
                return
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=daily["date"], y=daily["cost_usd"],
                mode="lines",
                line=dict(color=chrome.ACCENT, width=2),
                hovertemplate="<b>%{x}</b><br>$%{y:,.0f}<extra></extra>",
            ))
            chrome.style_fig(fig, height=220)
            chrome.plot(fig)

            # CSV download button
            csv_data = daily.to_csv(index=False)
            ui.button("Download CSV", icon="download",
                      on_click=lambda: ui.download(
                          csv_data.encode(), "daily_spend_trend.csv")
                      ).props("dense flat no-caps").classes("mt-2").style(
                          f"color:{chrome.INK_MUTED}")

        _daily_chart()
        apply_btn.on_click(lambda: _daily_chart.refresh())


# ═══════════════════════════════════════════════════════════════════════════════
# 01 Warehouse / Compute
# ═══════════════════════════════════════════════════════════════════════════════

def _warehouse_compute(data: Any = None) -> None:
    chrome.section_title("Warehouse / Compute")

    # ── Filter controls ────────────────────────────────────────────────────
    from datetime import date as _d
    _today = _d.today()
    _month_start = _today.replace(day=1).isoformat()
    state: dict[str, str] = {"start": _month_start, "end": _today.isoformat(),
                             "rollup": "day"}

    with ui.row().classes("items-end gap-4 w-full mb-4"):
        start_input = (
            ui.input("Start date", value=state["start"])
            .props("dense outlined type=date")
            .classes("w-40")
            .style(f"color:{chrome.INK_PRIMARY}")
        )
        end_input = (
            ui.input("End date", value=state["end"])
            .props("dense outlined type=date")
            .classes("w-40")
            .style(f"color:{chrome.INK_PRIMARY}")
        )
        rollup_select = (
            ui.select(
                options=["day", "week", "month", "year"],
                value=state["rollup"],
                label="Roll-up",
            )
            .props("dense outlined")
            .classes("w-32")
            .style(f"color:{chrome.INK_PRIMARY}")
        )
        apply_btn = ui.button("Apply", icon="refresh").props(
            "dense no-caps"
        ).style(f"background:{chrome.ACCENT};color:#fff;")

    # ── Content container (refreshable) ────────────────────────────────────
    content = ui.column().classes("w-full gap-6")

    def _refresh() -> None:
        state["start"] = start_input.value or state["start"]
        state["end"] = end_input.value or state["end"]
        state["rollup"] = rollup_select.value or state["rollup"]
        content.clear()
        with content:
            _render_wh_content(
                state["start"], state["end"], state["rollup"], data
            )

    apply_btn.on_click(_refresh)
    # Initial render
    with content:
        _render_wh_content(
            state["start"], state["end"], state["rollup"], data
        )


def _render_wh_content(start: str, end: str, rollup: str, data: Any = None) -> None:
    """Render warehouse compute content for given filters."""
    _d = data if data is not None else sf_data
    # Spend trend by warehouse (rolled up)
    with chrome.panel():
        chrome.panel_title(
            f"Spend by Warehouse ({rollup} grain, {start} to {end})"
        )
        trend = _d.warehouse_spend_filtered(start, end, rollup)
        if not trend.empty:
            # Aggregate to total per period for line chart
            totals = trend.groupby("period", as_index=False).agg(
                {"cost_usd": "sum"}
            )
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=totals["period"], y=totals["cost_usd"],
                mode="lines+markers",
                line=dict(color=chrome.ACCENT, width=2),
                marker=dict(size=4), name="Total",
            ))
            chrome.style_fig(fig, height=220)
            chrome.plot(fig)

    # Summary bar chart
    with chrome.panel():
        chrome.panel_title("Warehouse Total Spend")
        by_wh = _d.warehouse_summary_filtered(start, end)
        if not by_wh.empty:
            fig = go.Figure()
            fig.add_trace(go.Bar(
                y=by_wh["warehouse_name"], x=by_wh["cost_usd"],
                orientation="h", marker_color=chrome.ACCENT,
            ))
            chrome.style_fig(fig, height=340, currency_axis="x")
            fig.update_layout(yaxis=dict(autorange="reversed"))
            chrome.plot(fig)

    # P01: Idle warehouses
    chrome.section_title("Idle or Underused Warehouses")
    idle = _d.idle_warehouses_filtered(start, end)
    if not idle.empty:
        chrome.searchable_table(
            idle, key="v_idle",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits", "avg_running", "avg_queued_load"],
            rename={
                "object_name": "Warehouse", "credits": "Credits",
                "cost_usd": "Cost", "avg_running": "Avg Running",
                "avg_queued_load": "Avg Queued",
                "severity": "Severity",
            },
        )
    else:
        ui.label("No idle warehouses in this period.").classes(
            "text-sm"
        ).style(f"color:{chrome.INK_MUTED}")

    # P03: Queue pressure
    ui.separator().classes("my-4")
    chrome.section_title("Queue Pressure")
    queue = _d.queue_pressure_filtered(start, end)
    if not queue.empty:
        chrome.searchable_table(
            queue, key="v_queue",
            search_col="object_name",
            num_cols=["queued_seconds"],
            int_cols=["queries"],
            rename={
                "object_name": "Warehouse",
                "queued_seconds": "Queued (sec)",
                "queries": "Queries",
            },
        )
    else:
        ui.label("No queue pressure detected.").classes(
            "text-sm"
        ).style(f"color:{chrome.INK_MUTED}")


# ═══════════════════════════════════════════════════════════════════════════════
# Serverless Compute
# ═══════════════════════════════════════════════════════════════════════════════

def _serverless_compute() -> None:
    chrome.section_title("Serverless Compute")
    chrome.section_caption(
        "Snowflake-managed serverless services — cost visibility and links to detailed views"
    )

    # Show current month serverless spend
    kpis = sf_data.kpi_summary()
    chrome.kpi_row([
        ("Serverless Compute", f"${kpis['serverless_compute_cost']:,.0f}",
         "Current month total"),
    ], columns=3)

    ui.markdown("""
**Serverless compute costs are distributed across specialized service tabs for detailed analysis:**
""").style(f"color:{chrome.INK_PRIMARY}")

    services = [
        ("Auto Clustering", "Automatic reclustering of tables",
         "Ingestion & Orch."),
        ("Snowpipe", "Continuous data loading",
         "Ingestion & Orch."),
        ("Serverless Tasks", "Scheduled and triggered task execution",
         "Ingestion & Orch."),
        ("Replication", "Cross-region and cross-account replication",
         "Data Movement"),
        ("Data Transfer / Egress", "Cross-cloud and cross-region data transfer",
         "Data Movement"),
        ("Search Optimization", "Search optimization service maintenance",
         "Query Performance"),
        ("Materialized Views", "Materialized view maintenance",
         "Query Performance"),
        ("Query Acceleration", "Query acceleration service",
         "Query Performance"),
    ]

    with chrome.panel():
        chrome.panel_title("Serverless Services Directory")
        columns = [
            {"name": "Service", "label": "Service",
             "field": "service", "align": "left"},
            {"name": "Description", "label": "Description",
             "field": "description", "align": "left"},
            {"name": "Details In", "label": "Details In",
             "field": "tab", "align": "left"},
        ]
        rows = [{"service": s, "description": d, "tab": f"→ {t}"}
                for s, d, t in services]
        ui.table(
            columns=columns, rows=rows,
        ).props("dense flat hide-pagination").classes("w-full").style(
            f"background:{chrome.SURFACE};color:{chrome.INK_PRIMARY};"
            "font-size:13px;")


# ═══════════════════════════════════════════════════════════════════════════════
# Heatmaps
# ═══════════════════════════════════════════════════════════════════════════════

def _heatmaps(data: Any = None) -> None:
    _d = data if data is not None else sf_data
    chrome.section_title("Credit Spend Heatmaps")
    chrome.section_caption(
        "Per-entity deviation analysis — red cells indicate outlier days "
        "relative to each user/warehouse's own baseline"
    )

    # ── User Credit Spend Heatmap ─────────────────────────────────────────
    with chrome.panel():
        chrome.panel_title("Top 10 Users — Daily Credit Spend (Last 21 Days)")

        user_filter = ui.select(
            options=["All Users", "Service Accounts", "Ad-hoc Users"],
            value="All Users", label="User Type",
        ).props("dense outlined").classes("w-48 mb-2").style(
            f"color:{chrome.INK_PRIMARY}")

        @ui.refreshable
        def _user_heatmap() -> None:
            import numpy as np
            utype = {"All Users": "all", "Service Accounts": "service",
                     "Ad-hoc Users": "adhoc"}[user_filter.value]
            user_credits = _d.top_users_daily_credits(10, utype)
            if user_credits.empty:
                ui.label("No data").style(f"color:{chrome.INK_MUTED}")
                return
            pivot = user_credits.pivot_table(
                index="user_name", columns="usage_date",
                values="daily_credits", aggfunc="sum", fill_value=0,
            )
            pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=False).index]

            row_means = pivot.mean(axis=1).values.reshape(-1, 1)
            row_stds = pivot.std(axis=1).values.reshape(-1, 1)
            row_stds = np.where(row_stds == 0, 1, row_stds)
            z_scores = (pivot.values - row_means) / row_stds

            hover_text = []
            for i, user in enumerate(pivot.index):
                row_text = []
                for j, d in enumerate(pivot.columns):
                    cr = pivot.values[i][j]
                    zs = z_scores[i][j]
                    row_text.append(
                        f"<b>{user}</b><br>Date: {str(d)[:10]}<br>"
                        f"Credits: {cr:.1f}<br>Deviation: {zs:.1f}σ")
                hover_text.append(row_text)

            fig_hm = go.Figure(go.Heatmap(
                z=z_scores,
                x=[str(d)[:10] for d in pivot.columns],
                y=pivot.index.tolist(),
                colorscale=[
                    [0.0, "#0d1b2a"], [0.25, "#1b4332"],
                    [0.50, "#40916c"], [0.70, "#f4a261"],
                    [0.85, "#e76f51"], [1.0, "#d62828"],
                ],
                zmid=0, zmin=-2, zmax=3,
                text=hover_text, hoverinfo="text",
                colorbar=dict(title="σ from mean", tickformat=".1f",
                              len=0.9, thickness=15),
                xgap=2, ygap=2,
            ))
            chrome.style_fig(fig_hm, height=380, has_legend=False,
                             currency_axis=None)
            fig_hm.update_layout(
                margin=dict(l=130, r=60, t=10, b=50),
                xaxis=dict(tickangle=-45, dtick=2, tickfont=dict(size=10)),
                yaxis=dict(tickfont=dict(size=11)),
            )
            chrome.plot(fig_hm)

        _user_heatmap()
        user_filter.on_value_change(lambda _: _user_heatmap.refresh())

    # ── Warehouse Credit Spend Heatmap ────────────────────────────────────
    import numpy as np
    wh_credits = _d.warehouse_daily_credits(10)
    if not wh_credits.empty:
        with chrome.panel():
            chrome.panel_title(
                "Top 10 Warehouses — Daily Credit Spend (Last 21 Days)")
            pivot_wh = wh_credits.pivot_table(
                index="warehouse_name", columns="usage_date",
                values="daily_credits", aggfunc="sum", fill_value=0,
            )
            pivot_wh = pivot_wh.loc[
                pivot_wh.sum(axis=1).sort_values(ascending=False).index]

            wh_means = pivot_wh.mean(axis=1).values.reshape(-1, 1)
            wh_stds = pivot_wh.std(axis=1).values.reshape(-1, 1)
            wh_stds = np.where(wh_stds == 0, 1, wh_stds)
            wh_z_scores = (pivot_wh.values - wh_means) / wh_stds

            wh_hover = []
            for i, wh in enumerate(pivot_wh.index):
                row_text = []
                for j, d in enumerate(pivot_wh.columns):
                    cr = pivot_wh.values[i][j]
                    zs = wh_z_scores[i][j]
                    row_text.append(
                        f"<b>{wh}</b><br>Date: {str(d)[:10]}<br>"
                        f"Credits: {cr:.1f}<br>Deviation: {zs:.1f}σ")
                wh_hover.append(row_text)

            fig_wh = go.Figure(go.Heatmap(
                z=wh_z_scores,
                x=[str(d)[:10] for d in pivot_wh.columns],
                y=pivot_wh.index.tolist(),
                colorscale=[
                    [0.0, "#0d1b2a"], [0.25, "#1b4332"],
                    [0.50, "#40916c"], [0.70, "#f4a261"],
                    [0.85, "#e76f51"], [1.0, "#d62828"],
                ],
                zmid=0, zmin=-2, zmax=3,
                text=wh_hover, hoverinfo="text",
                colorbar=dict(title="σ from mean", tickformat=".1f",
                              len=0.9, thickness=15),
                xgap=2, ygap=2,
            ))
            chrome.style_fig(fig_wh, height=380, has_legend=False,
                             currency_axis=None)
            fig_wh.update_layout(
                margin=dict(l=130, r=60, t=10, b=50),
                xaxis=dict(tickangle=-45, dtick=2, tickfont=dict(size=10)),
                yaxis=dict(tickfont=dict(size=11)),
            )
            chrome.plot(fig_wh)


# ═══════════════════════════════════════════════════════════════════════════════
# 02 Query Performance
# ═══════════════════════════════════════════════════════════════════════════════

def _query_performance() -> None:
    chrome.section_title("Query Performance")

    # Q00: Attributed cost
    chrome.section_title("Top Query Patterns by Cost")
    attr = sf_data.query_attributed_cost()
    if not attr.empty:
        chrome.searchable_table(
            attr, key="v_q00",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits", "qas_credits"],
            int_cols=["queries"],
            rename={
                "object_name": "Query Hash", "credits": "Credits",
                "cost_usd": "Cost", "queries": "Runs",
                "qas_credits": "QAS Cr", "warehouse": "Warehouse",
            },
            max_rows=30,
        )

    # Q01: Expensive patterns
    ui.separator().classes("my-4")
    chrome.section_title("Expensive Query Patterns")
    exp = sf_data.expensive_query_patterns()
    if not exp.empty:
        chrome.searchable_table(
            exp, key="v_q01",
            search_col="object_name",
            num_cols=["tb_scanned", "gb_remote_spill", "max_elapsed_sec"],
            int_cols=["queries"],
            rename={
                "object_name": "Query Hash", "queries": "Runs",
                "tb_scanned": "Scanned TB",
                "gb_remote_spill": "Spill GB",
                "max_elapsed_sec": "Max Elapsed(s)",
                "warehouse": "Warehouse",
            },
            max_rows=30,
        )

    # Q09: Cache reuse
    ui.separator().classes("my-4")
    chrome.section_title("Cache Reuse Opportunities")
    cache = sf_data.cache_reuse_opportunity()
    if not cache.empty:
        chrome.searchable_table(
            cache, key="v_q09",
            search_col="object_name",
            num_cols=["tb_scanned", "avg_cache_pct"],
            int_cols=["queries", "warehouses"],
            rename={
                "object_name": "Query Hash", "queries": "Runs",
                "tb_scanned": "Scanned TB",
                "avg_cache_pct": "Cache %",
                "warehouses": "Warehouses",
            },
            max_rows=30,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 03 Storage
# ═══════════════════════════════════════════════════════════════════════════════

def _storage() -> None:
    chrome.section_title("Storage")

    # S01: Trend
    trend = sf_data.storage_trend()
    if not trend.empty:
        with chrome.panel():
            chrome.panel_title("Account Storage Trend")
            fig = go.Figure()
            for col, color, name in [
                ("table_tb", chrome.ACCENT, "Table"),
                ("stage_tb", chrome.OPPORTUNITY, "Stage"),
                ("failsafe_tb", chrome.WASTE, "Fail-safe"),
            ]:
                fig.add_trace(go.Scatter(
                    x=trend["date"], y=trend[col], mode="lines",
                    name=name, line=dict(color=color, width=2),
                    stackgroup="s",
                ))
            chrome.style_fig(
                fig, height=240, has_legend=True, currency_axis=None
            )
            fig.update_yaxes(ticksuffix=" TB")
            chrome.plot(fig)

    # S02: Top tables
    ui.separator().classes("my-4")
    chrome.section_title("Largest Tables")
    tables = sf_data.top_tables()
    if not tables.empty:
        chrome.searchable_table(
            tables, key="v_s02",
            search_col="object_name",
            num_cols=[
                "active_gb", "time_travel_gb",
                "failsafe_gb", "clone_gb",
            ],
            rename={
                "object_name": "Table", "active_gb": "Active GB",
                "time_travel_gb": "TT GB", "failsafe_gb": "FS GB",
                "clone_gb": "Clone GB", "transient": "Transient",
            },
            max_rows=20,
        )

    # Top 25 Tables — Storage Breakdown (stacked bar)
    top_tables = sf_data.top_tables_storage(25)
    if not top_tables.empty:
        ui.separator().classes("my-4")
        with chrome.panel():
            chrome.panel_title("Top 25 Tables — Storage Breakdown")
            fig_st = go.Figure()
            names = top_tables["table_name"].apply(
                lambda x: x.split(".")[-1] if "." in x else x
            ).tolist()
            fig_st.add_trace(go.Bar(
                y=names, x=top_tables["active_gb"], name="Active",
                orientation="h", marker_color=chrome.ACCENT,
            ))
            fig_st.add_trace(go.Bar(
                y=names, x=top_tables["time_travel_gb"], name="Time Travel",
                orientation="h", marker_color=chrome.OPPORTUNITY,
            ))
            fig_st.add_trace(go.Bar(
                y=names, x=top_tables["failsafe_gb"], name="Failsafe",
                orientation="h", marker_color=chrome.WASTE,
            ))
            fig_st.update_layout(barmode="stack")
            chrome.style_fig(fig_st, height=600, has_legend=True,
                             currency_axis=None)
            fig_st.update_xaxes(title_text="GB")
            fig_st.update_yaxes(autorange="reversed")
            fig_st.update_layout(margin=dict(l=160, r=20, t=30, b=40))
            chrome.plot(fig_st)

        # Account Storage Breakdown donut
        total_active = top_tables["active_gb"].sum()
        total_tt = top_tables["time_travel_gb"].sum()
        total_fs = top_tables["failsafe_gb"].sum()
        with chrome.panel():
            chrome.panel_title("Account Storage Breakdown")
            fig_sb = go.Figure(go.Pie(
                labels=["Active Storage", "Time Travel", "Failsafe"],
                values=[total_active, total_tt, total_fs],
                hole=0.45,
                marker_colors=[chrome.ACCENT, chrome.OPPORTUNITY, chrome.WASTE],
                hovertemplate="<b>%{label}</b><br>%{value:,.0f} GB<br>"
                              "%{percent}<extra></extra>",
                textinfo="label+percent",
                textposition="outside",
            ))
            chrome.style_fig(fig_sb, height=350, has_legend=False,
                             currency_axis=None)
            fig_sb.update_layout(margin=dict(l=20, r=20, t=20, b=20))
            chrome.plot(fig_sb)


# ═══════════════════════════════════════════════════════════════════════════════
# 04 Lakehouse / Iceberg Table
# ═══════════════════════════════════════════════════════════════════════════════

def _lakehouse_iceberg() -> None:
    chrome.section_title("Lakehouse / Iceberg Table")
    ui.label(
        "Iceberg checks (IB01–IB05) require catalog_linked_database "
        "and iceberg table metadata. Synthetic data covers the cost "
        "signal via metering_daily_history (service_type breakdown)."
    ).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


# ═══════════════════════════════════════════════════════════════════════════════
# 05 Data Design
# ═══════════════════════════════════════════════════════════════════════════════

def _data_design() -> None:
    chrome.section_title("Data Design")

    # D01: Serverless optimization spend
    chrome.section_title("Automatic Clustering Cost")
    clust = sf_data.serverless_optimization_spend()
    if not clust.empty:
        chrome.searchable_table(
            clust, key="v_d01",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits"],
            rename={
                "object_name": "Table",
                "service_type": "Service",
                "credits": "Credits", "cost_usd": "Cost",
            },
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 06 Ingestion and Orchestration
# ═══════════════════════════════════════════════════════════════════════════════

def _ingestion_orchestration() -> None:
    chrome.section_title("Ingestion and Orchestration")

    # I01: Snowpipe
    chrome.section_title("Snowpipe Cost & Efficiency")
    pipes = sf_data.snowpipe_cost()
    if not pipes.empty:
        chrome.searchable_table(
            pipes, key="v_i01",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits", "gb_inserted"],
            int_cols=["files"],
            rename={
                "object_name": "Pipe", "credits": "Credits",
                "cost_usd": "Cost", "gb_inserted": "GB In",
                "files": "Files",
            },
        )

    # I03: Serverless tasks
    ui.separator().classes("my-4")
    chrome.section_title("Serverless Task Costs")
    tasks = sf_data.serverless_task_costs()
    if not tasks.empty:
        chrome.searchable_table(
            tasks, key="v_i03",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits"],
            rename={
                "object_name": "Task", "credits": "Credits",
                "cost_usd": "Cost",
            },
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 07 AI and Cortex
# ═══════════════════════════════════════════════════════════════════════════════

def _ai_cortex() -> None:
    chrome.section_title("AI and Cortex")

    ai = sf_data.ai_spend_summary()
    kpis = sf_data.kpi_summary()
    ai_pct = round(
        ai["ai_cost"] / max(kpis["total_cost"], 1) * 100, 1
    )
    chrome.kpi_row([
        ("AI & Cortex Spend", f"${ai['ai_cost']:,.0f}",
         f"{ai['ai_credits']:,.0f} credits"),
        ("% of Total", f"{ai_pct}%", "AI share"),
        ("Annualized", f"${ai['ai_cost'] * 12:,.0f}", "projected"),
    ], columns=3)

    # AI daily trend
    with chrome.panel():
        chrome.panel_title("AI Spend Daily Trend")
        ai_daily = sf_data.ai_spend_by_day()
        if not ai_daily.empty:
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=ai_daily["date"], y=ai_daily["cost_usd"],
                mode="lines+markers",
                line=dict(color="#9085e9", width=2),
                marker=dict(size=4),
            ))
            chrome.style_fig(fig, height=200)
            chrome.plot(fig)

    # AI01: Service metering
    ui.separator().classes("my-4")
    chrome.section_title("AI Service Credits")
    svc = sf_data.ai_service_metering()
    if not svc.empty:
        chrome.searchable_table(
            svc, key="v_ai01",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits"],
            rename={
                "object_name": "Service", "credits": "Credits",
                "cost_usd": "Cost", "entity_type": "Type",
            },
        )

    # AI02: Function usage
    ui.separator().classes("my-4")
    chrome.section_title("Cortex AI Function Usage")
    funcs = sf_data.ai_function_usage()
    if not funcs.empty:
        chrome.searchable_table(
            funcs, key="v_ai02",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits"],
            int_cols=[
                "calls", "tokens_sent", "tokens_received", "users",
            ],
            rename={
                "object_name": "Function:Model",
                "credits": "Credits", "cost_usd": "Cost",
                "calls": "Calls", "tokens_sent": "Tokens In",
                "tokens_received": "Tokens Out",
                "users": "Users",
            },
        )

    # AI03: Cortex Search
    ui.separator().classes("my-4")
    chrome.section_title("Cortex Search Services")
    search = sf_data.ai_search_daily()
    if not search.empty:
        chrome.searchable_table(
            search, key="v_ai03",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits"],
            int_cols=["tokens"],
            rename={
                "object_name": "Service",
                "consumption_type": "Type",
                "credits": "Credits", "cost_usd": "Cost",
                "tokens": "Tokens",
            },
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 08 Data Movement and Availability
# ═══════════════════════════════════════════════════════════════════════════════

def _data_movement() -> None:
    chrome.section_title("Data Movement and Availability")

    # T01: Data transfer
    chrome.section_title("Data Transfer Drivers")
    transfers = sf_data.data_transfer_drivers()
    if not transfers.empty:
        chrome.searchable_table(
            transfers, key="v_t01",
            search_col="object_name",
            num_cols=["tb_transferred"],
            rename={
                "object_name": "Transfer Path",
                "tb_transferred": "TB Transferred",
            },
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 09 Snowpark SPCS and Openflow
# ═══════════════════════════════════════════════════════════════════════════════

def _spcs_openflow() -> None:
    chrome.section_title("Snowpark SPCS and Openflow")
    ui.label(
        "SPCS cost is tracked in the All Service Profile (Overview tab) "
        "under SNOWPARK_CONTAINER_SERVICES. Detailed compute pool "
        "metrics (P04, P11, P12) require SPCS-specific system views."
    ).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


# ═══════════════════════════════════════════════════════════════════════════════
# 10 Governance
# ═══════════════════════════════════════════════════════════════════════════════

def _governance() -> None:
    chrome.section_title("Governance")

    # G01: Unattributed spend
    chrome.section_title("Unattributed Warehouse Spend")
    unattr = sf_data.unattributed_spend()
    if not unattr.empty:
        total_u = unattr["cost_usd"].sum()
        chrome.kpi_row([
            ("Unattributed Cost", f"${total_u:,.0f}",
             f"{len(unattr)} warehouses"),
            ("Missing Owner",
             f"{len(unattr[unattr['owner'] == '<missing>'])}",
             "warehouses"),
            ("Missing Cost Center",
             f"{len(unattr[unattr['cost_center'] == '<missing>'])}",
             "warehouses"),
    ], columns=3)

        chrome.searchable_table(
            unattr, key="v_g01",
            search_col="object_name",
            money_cols=["cost_usd"],
            num_cols=["credits"],
            rename={
                "object_name": "Warehouse", "credits": "Credits",
                "cost_usd": "Cost", "owner": "Owner",
                "cost_center": "Cost Center",
            },
        )
