"""Per-provider FOCUS spend — one page per provider, rendered from its GOLD group.

Generalizes the original AWS board: each provider's GOLD lives in its own group
schema (``aws.*``, ``databricks.*``, …), so the page reads ``<group>.<view>`` with
no ``provider_name`` filter (the files are already provider-scoped). ``render`` is
bound per provider in ``app.py``; widget keys are namespaced by group so two
provider pages don't collide in session state.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data
from auralake.dashboard.theme import (
    PALETTE,
    compact_money,
    kpi_cards,
    month_filter,
    plotly,
    shadcn_table,
    style_fig,
)


def render(group: str, label: str) -> None:
    """Render the FOCUS spend page for one provider group (``group`` schema, ``label`` UI)."""
    st.title(f"{label} FOCUS spend")
    st.caption(f"{label} net cost over time and the SKUs driving it, in the FOCUS format.")
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    bill = gold_df(f'SELECT * FROM "{group}".monthly_bill ORDER BY charge_month')
    if bill.empty:
        st.info(f"No {label} rows found. Enable a {label} connector in connections.yml.")
        return

    months = bill["charge_month"].astype(str).tolist()
    month = month_filter(months, key=f"{group}_month") or max(months)
    sel = bill[bill["charge_month"].astype(str) == month]
    n_services = gold_df(
        f'SELECT count(DISTINCT service_name) AS n FROM "{group}".spend_by_service_month'
    )["n"].iloc[0]

    kpi_cards(
        [
            (f"{label} net · {month}", compact_money(sel["net_cost"].sum()), "selected month"),
            (
                f"{label} net · all time",
                compact_money(bill["net_cost"].sum()),
                f"{len(months)} months",
            ),
            (f"{label} savings · all time", compact_money(bill["savings"].sum()), "vs list"),
            (f"{label} services", str(int(n_services)), "distinct services"),
        ],
        key=group,
    )

    st.divider()
    _trend(group, label, bill)
    st.write("")
    _services_skus(group, label, month)
    st.write("")
    _tags(group, label, month)


def _trend(group: str, label: str, bill: pd.DataFrame) -> None:
    left, right = st.columns([2, 1])
    with left:
        trend = gold_df(
            f'SELECT charge_day, net_cost FROM "{group}".spend_trend_daily ORDER BY charge_day'
        )
        if trend.empty:
            st.info(f"No daily {label} rows.")
        else:
            fig = px.area(
                trend,
                x="charge_day",
                y="net_cost",
                labels={"charge_day": "", "net_cost": "Net cost"},
            )
            fig.update_traces(line_color="#2E86AB", fillcolor="rgba(46,134,171,0.18)")
            plotly(style_fig(fig), title=f"Daily {label} spend", key=f"{group}_trend")
    with right:
        cats = gold_df(
            'SELECT service_category, SUM(net_cost) AS net_cost '
            f'FROM "{group}".spend_by_service_month '
            "GROUP BY service_category ORDER BY net_cost DESC"
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
                key=f"{group}_pie",
            )

    bar = bill.copy()
    bar["month"] = pd.to_datetime(bar["charge_month"]).dt.strftime("%Y-%m")
    fig = px.bar(bar, x="month", y="net_cost", labels={"month": "", "net_cost": "Net cost"})
    fig.update_traces(marker_color=PALETTE[1])
    plotly(style_fig(fig), title=f"Monthly {label} bill (net)", key=f"{group}_monthly")


def _services_skus(group: str, label: str, month: str) -> None:
    left, right = st.columns(2)
    with left:
        st.markdown(f"##### Top {label} services")
        st.caption(month)
        services = gold_df(
            "SELECT service_name, service_category, SUM(net_cost) AS net_cost "
            f'FROM "{group}".spend_by_service_month '
            f"WHERE charge_month = '{month}' GROUP BY service_name, service_category "
            "ORDER BY net_cost DESC LIMIT 20"
        )
        if services.empty:
            st.info("No service rows for this month.")
        else:
            shadcn_table(
                services,
                key=f"{group}_services",
                money_cols=["net_cost"],
                rename={
                    "service_name": "Service",
                    "service_category": "Category",
                    "net_cost": "Net cost",
                },
            )
    with right:
        st.markdown(f"##### Top {label} SKUs")
        st.caption(month)
        skus = gold_df(
            "SELECT service_name, sku_id, SUM(net_cost) AS net_cost, "
            "SUM(consumed_quantity) AS quantity, max(consumed_unit) AS unit "
            f'FROM "{group}".spend_by_sku_month '
            f"WHERE charge_month = '{month}' GROUP BY service_name, sku_id "
            "ORDER BY net_cost DESC LIMIT 20"
        )
        if skus.empty:
            st.info("No SKU rows for this month.")
        else:
            shadcn_table(
                skus,
                key=f"{group}_skus",
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


def _tags(group: str, label: str, month: str) -> None:
    st.markdown("##### Spend by tag")
    st.caption(f"Top {label} tags by net cost · {month}")
    tags = gold_df(
        f'SELECT tag_key, tag_value, SUM(net_cost) AS net_cost FROM "{group}".spend_by_tag_month '
        f"WHERE charge_month = '{month}' "
        "GROUP BY tag_key, tag_value ORDER BY net_cost DESC LIMIT 20"
    )
    if tags.empty:
        st.info("No tag rows for this month.")
        return
    shadcn_table(
        tags,
        key=f"{group}_tags",
        money_cols=["net_cost"],
        rename={"tag_key": "Tag", "tag_value": "Value", "net_cost": "Net cost"},
    )
