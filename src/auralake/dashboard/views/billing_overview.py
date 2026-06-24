"""Billing overview — headline spend, savings, and where the money goes.

Layout mirrors the original Grafana ``billing_overview`` board (recovered from
git history): a KPI stat row, then everything on one dense scroll — a monthly
provider trend beside a list-vs-effective line, a by-service donut beside a
top-SKU bar, a team-tag table, a daily provider trend, and an SKU
month-over-month heatmap — all over the same GOLD views.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data
from auralake.dashboard.theme import (
    PALETTE,
    compact_money,
    heatmap_table,
    kpi_cards,
    month_filter,
    plotly,
    shadcn_table,
    style_fig,
)


def render() -> None:
    st.title("Billing overview")
    st.caption("Net cloud spend across all connected providers, by charge month.")
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    bill = gold_df("SELECT * FROM gold.monthly_bill ORDER BY charge_month")
    if bill.empty:
        st.info("No billing rows found.")
        return

    months = bill["charge_month"].astype(str).tolist()
    month = month_filter(months, key="billing_month") or max(months)

    _kpis(bill, month)
    st.divider()
    _trend_row(bill)
    st.write("")
    _composition_row()
    st.write("")
    _daily_trend()
    st.write("")
    _sku_movement(month)


def _kpis(bill: pd.DataFrame, month: str) -> None:
    sel = bill[bill["charge_month"].astype(str) == month]
    months = set(bill["charge_month"].astype(str))

    earlier = sorted(m for m in months if m < month)
    delta = ""
    if earlier:
        prev = bill[bill["charge_month"].astype(str) == earlier[-1]]["net_cost"].sum()
        if prev:
            delta = f"{(sel['net_cost'].sum() - prev) / prev:+.1%} vs prior month"

    list_cost = sel["list_cost"].sum()
    savings = sel["savings"].sum()
    savings_sub = f"{savings / list_cost:.0%} off list" if list_cost else ""
    kpi_cards(
        [
            (f"Net cost · {month}", compact_money(sel["net_cost"].sum()), delta),
            ("List cost", compact_money(list_cost), "before discounts"),
            ("Savings", compact_money(savings), savings_sub),
            ("Providers", str(sel["provider_name"].nunique()), "billing sources"),
        ],
        key="billing",
    )


def _trend_row(bill: pd.DataFrame) -> None:
    bar = bill.copy()
    bar["month"] = pd.to_datetime(bar["charge_month"]).dt.strftime("%Y-%m")
    left, right = st.columns(2)
    with left:
        plotly(
            style_fig(
                px.bar(
                    bar,
                    x="month",
                    y="net_cost",
                    color="provider_name",
                    labels={"month": "", "net_cost": "Net cost", "provider_name": ""},
                )
            ),
            title="Monthly net cost by provider",
            key="billing_bar",
        )
    with right:
        # savings_summary_month is one row per provider per month — roll up to month.
        savings_df = gold_df(
            "SELECT charge_month, SUM(list_cost) AS list_cost, "
            "SUM(effective_cost) AS effective_cost FROM gold.savings_summary_month "
            "GROUP BY charge_month ORDER BY charge_month"
        )
        if savings_df.empty:
            st.info("No savings rows.")
        else:
            savings_df["month"] = pd.to_datetime(savings_df["charge_month"]).dt.strftime("%Y-%m")
            fig = px.line(
                savings_df,
                x="month",
                y=["list_cost", "effective_cost"],
                markers=True,
                labels={"month": "", "value": "Cost", "variable": ""},
            )
            fig.for_each_trace(
                lambda t: t.update(name="List cost" if t.name == "list_cost" else "Effective cost")
            )
            plotly(style_fig(fig), title="List vs effective cost", key="billing_savings")


def _composition_row() -> None:
    left, right = st.columns(2)
    with left:
        services = gold_df(
            "SELECT service_name, SUM(net_cost) AS net_cost FROM gold.spend_by_service_month "
            "GROUP BY service_name ORDER BY net_cost DESC"
        )
        if services.empty:
            st.info("No service rows.")
        else:
            fig = px.pie(
                services,
                names="service_name",
                values="net_cost",
                hole=0.55,
                color_discrete_sequence=PALETTE,
            )
            fig.update_traces(textposition="inside", textinfo="percent")
            plotly(
                style_fig(fig, currency_axis=None),
                title="Where it goes — by service (all time)",
                key="billing_pie",
            )
    with right:
        skus = gold_df(
            "SELECT sku_id, SUM(net_cost) AS net_cost FROM gold.spend_by_sku_month "
            "GROUP BY sku_id ORDER BY net_cost DESC LIMIT 10"
        )
        if skus.empty:
            st.info("No SKU rows.")
        else:
            fig = px.bar(
                skus.sort_values("net_cost"),
                x="net_cost",
                y="sku_id",
                orientation="h",
                labels={"net_cost": "Net cost", "sku_id": ""},
            )
            fig.update_traces(marker_color=PALETTE[1])
            plotly(
                style_fig(fig, currency_axis="x"),
                title="Top SKUs by spend (all time)",
                key="billing_topsku",
            )

    tags = gold_df(
        "SELECT tag_value AS team, SUM(net_cost) AS net_cost FROM gold.spend_by_tag_month "
        "WHERE tag_key = 'team' GROUP BY tag_value ORDER BY net_cost DESC LIMIT 15"
    )
    if not tags.empty:
        st.markdown("##### Spend by team tag (all time)")
        shadcn_table(
            tags, key="billing_tags", money_cols=["net_cost"], rename={"net_cost": "Net cost"}
        )


def _daily_trend() -> None:
    daily = gold_df(
        "SELECT charge_day, provider_name, net_cost FROM gold.spend_trend_daily "
        "ORDER BY charge_day"
    )
    if daily.empty:
        return
    plotly(
        style_fig(
            px.line(
                daily,
                x="charge_day",
                y="net_cost",
                color="provider_name",
                labels={"charge_day": "", "net_cost": "Net cost", "provider_name": ""},
            )
        ),
        title="Daily spend trend by provider",
        key="billing_daily",
    )


def _sku_movement(month: str) -> None:
    st.markdown("##### SKU month-over-month")
    st.caption(f"Top SKUs by spend · {month} vs prior month")
    mom = gold_df(
        "SELECT sku_id, net_cost, prev_cost, cost_delta, cost_pct_change "
        f"FROM gold.sku_month_over_month WHERE charge_month = '{month}' "
        "ORDER BY net_cost DESC LIMIT 25"
    )
    if mom.empty:
        st.info("No SKU movement for this month.")
        return
    heatmap_table(
        mom,
        heat_col="cost_pct_change",
        money_cols=["net_cost", "prev_cost", "cost_delta"],
        rename={
            "sku_id": "SKU",
            "net_cost": "This month",
            "prev_cost": "Prior month",
            "cost_delta": "Δ cost",
            "cost_pct_change": "Δ %",
        },
    )
