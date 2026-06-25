"""Per-provider FOCUS spend — one page per provider, rendered from its GOLD group.

Each provider's GOLD lives in its own group schema (``aws.*``, ``databricks.*``, …),
so the page reads ``<group>.<view>`` with no ``provider_name`` filter. A single
sidebar **from→to date range** drives every panel below it. ``render`` is bound per
provider in ``app.py``; widget keys are namespaced by group so two provider pages
don't collide in session state.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data
from auralake.dashboard.theme import (
    PALETTE,
    compact_money,
    date_range,
    heatmap_table,
    html_table,
    kpi_cards,
    plotly,
    style_fig,
)


def _d(value: object) -> date:
    """Coerce a DuckDB/pandas date-ish scalar to a plain ``date``."""
    ts = pd.Timestamp(value)
    return date(ts.year, ts.month, ts.day)


def render(group: str, label: str) -> None:
    """Render the FOCUS spend page for one provider group (``group`` schema, ``label`` UI)."""
    st.title(f"{label} FOCUS spend")
    st.caption(f"{label} net cost over the chosen window and the SKUs driving it.")
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    bounds = gold_df(
        f'SELECT min(charge_day) AS lo, max(charge_day) AS hi FROM "{group}".spend_trend_daily'
    )
    if bounds.empty or pd.isna(bounds["lo"].iloc[0]):
        st.info(f"No {label} rows found. Enable a {label} connector in connections.yml.")
        return

    lo, hi = _d(bounds["lo"].iloc[0]), _d(bounds["hi"].iloc[0])
    # Default the window to open on the first month with material spend, so a one-day
    # sliver at the very start (e.g. a Dec 31 charge) doesn't anchor the range there —
    # the user can still drag back to `lo`.
    first = gold_df(
        f'SELECT min(charge_month) AS m FROM "{group}".monthly_bill WHERE abs(net_cost) >= 1'
    )
    default_lo = _d(first["m"].iloc[0]) if not first.empty and pd.notna(first["m"].iloc[0]) else lo
    start, end = date_range(lo, hi, key=f"{group}_range", default_lo=default_lo)
    sm = start.replace(day=1)  # month-grain views key on the first of the month

    _kpis(group, label, start, end, sm)
    st.divider()
    _trend(group, label, start, end)
    _spend_pivot(group, end, sm)
    _savings(group, label, end, sm)
    _sku_mom(group, end)
    _tags(group, label, end, sm)


def _kpis(group: str, label: str, start: date, end: date, sm: date) -> None:
    agg = gold_df(
        "SELECT coalesce(sum(net_cost),0) AS net, coalesce(sum(list_cost),0) AS lst, "
        f'coalesce(sum(savings),0) AS sav FROM "{group}".monthly_bill '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    ).iloc[0]
    net, lst, sav = float(agg["net"]), float(agg["lst"]), float(agg["sav"])
    disc = f"{100 * sav / lst:.1f}%" if lst else "—"
    span = f"{start:%b %d} → {end:%b %d}"
    kpi_cards(
        [
            (f"{label} net", compact_money(net), span),
            (f"{label} list", compact_money(lst), "before discounts"),
            (f"{label} savings", compact_money(sav), "vs list"),
            ("Realized discount", disc, "off list"),
        ],
        key=group,
    )


def _trend(group: str, label: str, start: date, end: date) -> None:
    trend = gold_df(
        f'SELECT charge_day, net_cost FROM "{group}".spend_trend_daily '
        f"WHERE charge_day >= '{start}' AND charge_day <= '{end}' ORDER BY charge_day"
    )
    if trend.empty:
        st.info(f"No daily {label} rows in range.")
        return
    fig = px.area(trend, x="charge_day", y="net_cost", labels={"charge_day": "", "net_cost": ""})
    fig.update_traces(line_color=PALETTE[1], fillcolor="rgba(46,134,171,0.18)")
    plotly(style_fig(fig), title=f"Daily {label} spend", key=f"{group}_trend")


def _spend_pivot(group: str, end: date, sm: date) -> None:
    """Top cost drivers as a <dim> × month spend matrix with reconciling row/col totals.

    Pivots on SKU for Databricks (its service names — JOBS, SQL — are too coarse; the
    SKU id carries the real detail) and on service for everyone else (where names like
    'Amazon EC2' are meaningful).
    """
    if group == "databricks":
        dim, view, label = "sku_id", "spend_by_sku_month", "SKU"
    else:
        dim, view, label = "service_name", "spend_by_service_month", "Service"

    st.markdown(f"##### {label}s × month — spend")
    df = gold_df(
        f"SELECT {dim} AS k, charge_month, sum(net_cost) AS net_cost "
        f'FROM "{group}".{view} '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        f"GROUP BY {dim}, charge_month"
    )
    if df.empty:
        st.info(f"No {label.lower()} rows in range.")
        return

    current = pd.Timestamp(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    pivot = df.pivot_table(
        index="k", columns="charge_month", values="net_cost", aggfunc="sum", fill_value=0.0
    )
    # Drop the uniform ENTERPRISE_ prefix Databricks puts on every SKU id.
    pivot.index = pivot.index.str.replace("ENTERPRISE_", "", regex=False)
    pivot = pivot.reindex(sorted(pivot.columns), axis=1)  # chronological months
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("Total", ascending=False)
    pivot.loc["Total"] = pivot.sum(axis=0)  # column totals reconcile with the row totals

    def _col(c: object) -> str:
        if c == "Total":
            return "Total"
        ts = pd.Timestamp(c)
        return f"{ts:%b %Y}" + (" (partial)" if ts == current else "")

    pivot.columns = [_col(c) for c in pivot.columns]
    out = pivot.reset_index().rename(columns={"k": label})
    for col in out.columns:
        if col != label:
            out[col] = out[col].map(lambda v: "" if not v else compact_money(v))
    html_table(out)


def _savings(group: str, label: str, end: date, sm: date) -> None:
    sv = gold_df(
        "SELECT charge_month, effective_cost, savings, list_cost, savings_pct "
        f'FROM "{group}".savings_summary_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' AND list_cost >= 1 "
        "ORDER BY charge_month"
    )
    if sv.empty:
        return
    latest = sv.iloc[-1]
    st.markdown("##### Bill: list vs effective")
    st.caption(f"Effective + savings = list. Latest month · {latest['savings_pct']:.1f}% off list.")
    melt = sv.copy()
    melt["month"] = pd.to_datetime(melt["charge_month"]).dt.strftime("%Y-%m")
    melt = melt.melt(
        id_vars="month",
        value_vars=["effective_cost", "savings"],
        var_name="component",
        value_name="cost",
    )
    melt["component"] = melt["component"].map(
        {"effective_cost": "Effective (paid)", "savings": "Savings (discount)"}
    )
    fig = px.bar(
        melt,
        x="month",
        y="cost",
        color="component",
        barmode="stack",  # stacked, so the full bar height = list cost (Grafana-style)
        color_discrete_map={"Effective (paid)": PALETTE[1], "Savings (discount)": PALETTE[4]},
        category_orders={"component": ["Effective (paid)", "Savings (discount)"]},
        labels={"month": "", "cost": "", "component": ""},
    )
    plotly(style_fig(fig), key=f"{group}_savings")


def _sku_mom(group: str, end: date) -> None:
    st.markdown("##### SKU month-over-month")
    # Compare the latest COMPLETE month (exclude the current, still-accruing month) so
    # we never pit a partial month against a full one. Honour the selected range's end.
    current = pd.Timestamp(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    months = gold_df(
        f'SELECT DISTINCT charge_month FROM "{group}".sku_month_over_month '
        f"WHERE charge_month <= '{end}' AND charge_month < '{current.date()}' "
        "ORDER BY charge_month DESC LIMIT 1"
    )
    if months.empty:
        st.caption("Not enough complete months in range to compare.")
        st.info("Need at least one full (non-current) month of data.")
        return
    cmp_month = pd.Timestamp(months.iloc[0]["charge_month"])
    prior = cmp_month - pd.DateOffset(months=1)
    st.caption(f"Top SKUs by net cost · {cmp_month:%b %Y} vs {prior:%b %Y}")
    mom = gold_df(
        "SELECT sku_id, net_cost, cost_delta, cost_pct_change "
        f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{cmp_month.date()}\' '
        "ORDER BY net_cost DESC LIMIT 20"
    )
    if mom.empty:
        st.info("No SKU movement rows for the latest complete month.")
        return
    heatmap_table(
        mom,
        heat_col="cost_pct_change",
        money_cols=["net_cost", "cost_delta"],
        rename={
            "sku_id": "SKU",
            "net_cost": "Net cost",
            "cost_delta": "Δ vs prior",
            "cost_pct_change": "MoM %",
        },
    )


def _tags(group: str, label: str, end: date, sm: date) -> None:
    keys = gold_df(
        f'SELECT tag_key, sum(net_cost) AS net FROM "{group}".spend_by_tag_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY tag_key ORDER BY net DESC"
    )
    if keys.empty:
        st.markdown("##### Spend by tag")
        st.info("No tagged spend in range.")
        return

    st.markdown("##### Spend by tag")
    options = keys["tag_key"].tolist()
    default = "team" if "team" in options else options[0]
    sel = st.selectbox(
        "Tag key",
        options=options,
        index=options.index(default),
        key=f"{group}_tagkey",
        label_visibility="collapsed",
    )
    st.caption(f"{label} spend broken down by the `{sel}` tag")
    tags = gold_df(
        f"SELECT tag_value, sum(net_cost) AS net_cost FROM \"{group}\".spend_by_tag_month "
        f"WHERE tag_key = '{sel}' AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY tag_value ORDER BY net_cost DESC LIMIT 20"
    )
    if tags.empty:
        st.info("No values for this tag in range.")
        return
    html_table(
        tags,
        money_cols=["net_cost"],
        rename={"tag_value": sel, "net_cost": "Net cost"},
    )
