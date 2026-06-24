"""AWS FOCUS — AWS spend trend and per-SKU breakdown."""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data


def render() -> None:
    st.title("AWS FOCUS spend")
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    bill = gold_df(
        "SELECT * FROM gold.monthly_bill WHERE provider_name = 'AWS' ORDER BY charge_month"
    )
    if bill.empty:
        st.info("No AWS rows found. Enable an AWS connector in connections.yml.")
        return

    c1, c2 = st.columns(2)
    c1.metric("AWS net cost (latest)", f"${bill['net_cost'].iloc[-1]:,.0f}")
    c2.metric("AWS net cost (all time)", f"${bill['net_cost'].sum():,.0f}")

    trend = gold_df(
        "SELECT charge_day, net_cost FROM gold.spend_trend_daily "
        "WHERE provider_name = 'AWS' ORDER BY charge_day"
    )
    if not trend.empty:
        st.plotly_chart(
            px.line(trend, x="charge_day", y="net_cost", title="AWS daily net cost"),
            use_container_width=True,
        )

    st.subheader("Top SKUs (latest month)")
    skus = gold_df(
        "SELECT service_name, sku_id, net_cost, consumed_quantity, consumed_unit "
        "FROM gold.spend_by_sku_month "
        "WHERE provider_name = 'AWS' "
        "AND charge_month = (SELECT max(charge_month) FROM gold.spend_by_sku_month "
        "WHERE provider_name = 'AWS') "
        "ORDER BY net_cost DESC LIMIT 20"
    )
    st.dataframe(skus, use_container_width=True, hide_index=True)
