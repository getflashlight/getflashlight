"""AWS FOCUS — AWS spend trend, service/SKU breakdown, and tags.

Layout mirrors the original Grafana ``aws_focus`` board (recovered from git
history): AWS stats, a daily trend + service-category donut, top-service and
top-SKU tables, the monthly bill bar, and a tag table.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data
from auralake.dashboard.theme import (
    PALETTE,
    kpi_cards,
    month_filter,
    plotly,
    shadcn_table,
    style_fig,
    tabs,
)


def render() -> None:
    st.title("AWS FOCUS spend")
    st.caption("AWS net cost over time and the SKUs driving it, in the FOCUS format.")
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    bill = gold_df(
        "SELECT * FROM gold.monthly_bill WHERE provider_name = 'AWS' ORDER BY charge_month"
    )
    if bill.empty:
        st.info("No AWS rows found. Enable an AWS connector in connections.yml.")
        return

    months = bill["charge_month"].astype(str).tolist()
    month = month_filter(months, key="aws_month") or max(months)
    sel = bill[bill["charge_month"].astype(str) == month]
    n_services = gold_df(
        "SELECT count(DISTINCT service_name) AS n FROM gold.spend_by_service_month "
        "WHERE provider_name = 'AWS'"
    )["n"].iloc[0]

    kpi_cards(
        [
            (f"AWS net · {month}", f"${sel['net_cost'].sum():,.0f}", "selected month"),
            ("AWS net · all time", f"${bill['net_cost'].sum():,.0f}", f"{len(months)} months"),
            ("AWS savings · all time", f"${bill['savings'].sum():,.0f}", "vs list"),
            ("AWS services", str(int(n_services)), "distinct services"),
        ],
        key="aws",
    )

    st.write("")
    active = tabs(["Trend", "Services & SKUs", "Tags"], key="aws_tabs")

    if active == "Trend":
        _trend(bill)
    elif active == "Services & SKUs":
        _services_skus(month)
    elif active == "Tags":
        _tags(month)


def _trend(bill: pd.DataFrame) -> None:
    left, right = st.columns([2, 1])
    with left:
        trend = gold_df(
            "SELECT charge_day, net_cost FROM gold.spend_trend_daily "
            "WHERE provider_name = 'AWS' ORDER BY charge_day"
        )
        if trend.empty:
            st.info("No daily AWS rows.")
        else:
            fig = px.area(
                trend,
                x="charge_day",
                y="net_cost",
                labels={"charge_day": "", "net_cost": "Net cost"},
            )
            fig.update_traces(line_color="#2E86AB", fillcolor="rgba(46,134,171,0.18)")
            plotly(style_fig(fig), title="Daily AWS spend", key="aws_trend")
    with right:
        cats = gold_df(
            "SELECT service_category, SUM(net_cost) AS net_cost FROM gold.spend_by_service_month "
            "WHERE provider_name = 'AWS' GROUP BY service_category ORDER BY net_cost DESC"
        )
        if cats.empty:
            st.info("No category rows.")
        else:
            fig = px.pie(
                cats,
                names="service_category",
                values="net_cost",
                hole=0.55,
                color_discrete_sequence=PALETTE,
            )
            fig.update_traces(textposition="inside", textinfo="percent")
            plotly(
                style_fig(fig, currency_axis=None),
                title="By service category",
                key="aws_pie",
            )

    bar = bill.copy()
    bar["month"] = pd.to_datetime(bar["charge_month"]).dt.strftime("%Y-%m")
    fig = px.bar(bar, x="month", y="net_cost", labels={"month": "", "net_cost": "Net cost"})
    fig.update_traces(marker_color=PALETTE[1])
    plotly(style_fig(fig), title="Monthly AWS bill (net)", key="aws_monthly")


def _services_skus(month: str) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown("##### Top AWS services")
        st.caption(month)
        services = gold_df(
            "SELECT service_name, service_category, SUM(net_cost) AS net_cost "
            "FROM gold.spend_by_service_month WHERE provider_name = 'AWS' "
            f"AND charge_month = '{month}' GROUP BY service_name, service_category "
            "ORDER BY net_cost DESC LIMIT 20"
        )
        if services.empty:
            st.info("No service rows for this month.")
        else:
            shadcn_table(
                services,
                key="aws_services",
                money_cols=["net_cost"],
                rename={
                    "service_name": "Service",
                    "service_category": "Category",
                    "net_cost": "Net cost",
                },
            )
    with right:
        st.markdown("##### Top AWS SKUs")
        st.caption(month)
        skus = gold_df(
            "SELECT service_name, sku_id, SUM(net_cost) AS net_cost, "
            "SUM(consumed_quantity) AS quantity, max(consumed_unit) AS unit "
            "FROM gold.spend_by_sku_month WHERE provider_name = 'AWS' "
            f"AND charge_month = '{month}' GROUP BY service_name, sku_id "
            "ORDER BY net_cost DESC LIMIT 20"
        )
        if skus.empty:
            st.info("No SKU rows for this month.")
        else:
            shadcn_table(
                skus,
                key="aws_skus",
                money_cols=["net_cost"],
                num_cols=["quantity"],
                rename={
                    "service_name": "Service",
                    "sku_id": "SKU",
                    "net_cost": "Net cost",
                    "quantity": "Quantity",
                    "unit": "Unit",
                },
            )


def _tags(month: str) -> None:
    st.caption(f"AWS spend by tag · {month}")
    tags = gold_df(
        "SELECT tag_key, tag_value, SUM(net_cost) AS net_cost FROM gold.spend_by_tag_month "
        f"WHERE provider_name = 'AWS' AND charge_month = '{month}' "
        "GROUP BY tag_key, tag_value ORDER BY net_cost DESC LIMIT 20"
    )
    if tags.empty:
        st.info("No tag rows for this month.")
        return
    shadcn_table(
        tags,
        key="aws_tags",
        money_cols=["net_cost"],
        rename={"tag_key": "Tag", "tag_value": "Value", "net_cost": "Net cost"},
    )
