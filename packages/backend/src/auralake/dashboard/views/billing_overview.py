"""Billing overview — headline spend, savings, and where the money goes."""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data


def render() -> None:
    st.title("Billing overview")
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    bill = gold_df("SELECT * FROM gold.monthly_bill ORDER BY charge_month")
    if bill.empty:
        st.info("No billing rows found.")
        return

    latest_month = bill["charge_month"].max()
    latest = bill[bill["charge_month"] == latest_month]
    c1, c2, c3 = st.columns(3)
    c1.metric(f"Net cost ({latest_month})", f"${latest['net_cost'].sum():,.0f}")
    c2.metric("List cost", f"${latest['list_cost'].sum():,.0f}")
    c3.metric("Savings", f"${latest['savings'].sum():,.0f}")

    st.plotly_chart(
        px.bar(
            bill,
            x="charge_month",
            y="net_cost",
            color="provider_name",
            title="Monthly net cost by provider",
        ),
        use_container_width=True,
    )

    savings = gold_df("SELECT * FROM gold.savings_summary_month ORDER BY charge_month")
    if not savings.empty:
        st.plotly_chart(
            px.line(
                savings,
                x="charge_month",
                y=["list_cost", "effective_cost"],
                title="List vs effective cost",
            ),
            use_container_width=True,
        )

    st.subheader("Top services (latest month)")
    services = gold_df(
        "SELECT provider_name, service_name, net_cost "
        "FROM gold.spend_by_service_month "
        "WHERE charge_month = (SELECT max(charge_month) FROM gold.spend_by_service_month) "
        "ORDER BY net_cost DESC LIMIT 20"
    )
    st.dataframe(services, use_container_width=True, hide_index=True)
