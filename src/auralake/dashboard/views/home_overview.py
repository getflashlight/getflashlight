"""Home — cross-provider spend headline and month-over-month movement."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from auralake.dashboard.context import global_range
from auralake.dashboard.data import (
    NO_DATA_MSG,
    attribution_coverage,
    gold_df,
    gold_last_updated,
    has_data,
    provider_label,
)
from auralake.dashboard.data import to_date as _d
from auralake.dashboard.summary import cross_provider_movers
from auralake.dashboard.theme import (
    SEMANTIC,
    compact_money,
    delta_variant,
    html_table,
    kpi_cards,
    panel,
    plotly,
    provider_card,
    provider_color,
    provider_color_map,
    section_caption,
    section_title,
    style_fig,
)
from auralake.transform.catalog import discover_provider_groups

if TYPE_CHECKING:
    from streamlit.navigation.page import StreamlitPage


def _headline_month(start: date, end: date) -> date | None:
    """Latest complete charge month within the sidebar range (excludes current partial month)."""
    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    sm = start.replace(day=1)
    cap = min(
        end.replace(day=1),
        (pd.Timestamp(current) - pd.DateOffset(months=1)).date(),
    )
    if cap < sm:
        return None
    months: list[date] = []
    for group in discover_provider_groups():
        df = gold_df(
            f'SELECT max(charge_month) AS m FROM "{group}".monthly_bill '
            f"WHERE charge_month >= '{sm}' AND charge_month <= '{cap}'"
        )
        if not df.empty and pd.notna(df["m"].iloc[0]):
            months.append(_d(df["m"].iloc[0]))
    return max(months) if months else None


def _provider_months(group: str, month: date, prior: date) -> tuple[float, float]:
    row = gold_df(
        f"SELECT coalesce(sum(net_cost) FILTER (WHERE charge_month = '{month}'), 0) AS cur, "
        f"coalesce(sum(net_cost) FILTER (WHERE charge_month = '{prior}'), 0) AS prev "
        f'FROM "{group}".monthly_bill'
    ).iloc[0]
    return float(row["cur"]), float(row["prev"])


def _provider_history(groups: list[str], start: date, end: date) -> pd.DataFrame:
    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    sm = start.replace(day=1)
    cap = min(end.replace(day=1), (pd.Timestamp(current) - pd.DateOffset(months=1)).date())
    frames: list[pd.DataFrame] = []
    for group in groups:
        label = provider_label(group)
        df = gold_df(
            f'SELECT charge_month, net_cost FROM "{group}".monthly_bill '
            f"WHERE charge_month >= '{sm}' AND charge_month <= '{cap}' ORDER BY charge_month"
        )
        if df.empty:
            continue
        df["provider"] = label
        df["group"] = group
        df["month"] = pd.to_datetime(df["charge_month"]).dt.strftime("%Y-%m")
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).sort_values("charge_month")


def render(
    *,
    tco_page: StreamlitPage | None = None,
    provider_pages: dict[str, StreamlitPage] | None = None,
) -> None:
    st.title("Cloud spend overview")
    updated = gold_last_updated()
    start, end = global_range()
    cap = "Total spend across your connected cloud providers."
    if updated:
        cap += f" Data updated · {updated:%Y-%m-%d %H:%M} UTC."
    st.caption(cap)
    if not has_data():
        st.info(NO_DATA_MSG)
        return

    groups = discover_provider_groups()
    if not groups:
        st.info(NO_DATA_MSG)
        return

    month = _headline_month(start, end)
    if month is None:
        st.info(
            "No complete billing month in the selected range — widen the sidebar dates "
            "or wait for the current month to finish."
        )
        return

    st.caption(
        f"Headline KPIs and charts use the latest **complete month in your range**: "
        f"**{month:%b %Y}** (sidebar: {start:%b %d, %Y} → {end:%b %d, %Y})."
    )

    prior = (pd.Timestamp(month) - pd.DateOffset(months=1)).date()
    rows: list[dict[str, object]] = []
    total_cur = total_prev = 0.0
    for group in groups:
        label = provider_label(group)
        cur, prev = _provider_months(group, month, prior)
        if not cur and not prev:
            continue
        delta = cur - prev
        pct = 100 * delta / prev if prev else None
        rows.append(
            {"group": group, "provider": label, "net_cost": cur, "delta": delta, "pct": pct}
        )
        total_cur += cur
        total_prev += prev

    if not rows:
        st.info("No provider spend rows for the latest complete month.")
        return

    breakdown = pd.DataFrame(rows).sort_values("net_cost", ascending=False)
    total_delta = total_cur - total_prev
    total_pct = f"{100 * total_delta / total_prev:+.1f}%" if total_prev else "—"
    sign = "↑" if total_delta >= 0 else "↓"

    attr = attribution_coverage(month)
    total_tco, unattr = attr if attr else (0.0, 0.0)
    kpi_row: list[tuple[str, str, str, str]] = [
        (f"Total · {month:%b %Y}", compact_money(total_cur), "net across providers", "default"),
        (
            "Change vs prior month",
            f"{'+' if total_delta >= 0 else '−'}{compact_money(abs(total_delta))}",
            f"{sign} {total_pct} vs {prior:%b %Y}",
            delta_variant(total_delta),
        ),
        (
            "TCO attribution",
            f"{100 * (1 - unattr / total_tco):.0f}% attributed" if total_tco else "—",
            f"{compact_money(unattr)} AWS unmapped" if attr and unattr else "Databricks + AWS TCO",
            "unattributed" if attr and unattr else "default",
        ),
    ]
    kpi_cards(kpi_row, key="home")

    chart_col, share_col = st.columns([8, 4], gap="medium")
    history = _provider_history(groups, start, end)
    with chart_col:
        if not history.empty:
            with panel(tone="teal", flush=True):
                section_title("Spend trend by provider", flush=True)
                section_caption("Stacked net cost per month — each color is a cloud provider.")
                colors = provider_color_map(
                    history["provider"].unique(), groups=history["group"].unique()
                )
                fig = px.bar(
                    history,
                    x="month",
                    y="net_cost",
                    color="provider",
                    color_discrete_map=colors,
                    labels={"month": "", "net_cost": "", "provider": ""},
                )
                fig.update_layout(barmode="stack")
                plotly(style_fig(fig, has_legend=True), key="home_trend")
    with share_col:
        with panel(tone="default", flush=True):
            section_title("Provider share", flush=True)
            section_caption(f"{month:%b %Y} net cost mix")
            colors = provider_color_map(breakdown["provider"], groups=breakdown["group"])
            pie = px.pie(
                breakdown,
                names="provider",
                values="net_cost",
                color="provider",
                color_discrete_map=colors,
                hole=0.45,
            )
            pie.update_traces(textposition="inside", textinfo="percent+label")
            plotly(style_fig(pie, has_legend=False, currency_axis=None), key="home_share")

    with panel(tone="default"):
        bridge_tab, movers_tab = st.tabs(["Month-over-month bridge", "Biggest movers"])
        with bridge_tab:
            section_caption(f"How each provider moved spend from {prior:%b %Y} to {month:%b %Y}.")
            labels = [f"{prior:%b %Y}"] + breakdown["provider"].tolist() + [f"{month:%b %Y}"]
            measures = ["absolute"] + ["relative"] * len(breakdown) + ["total"]
            values = [total_prev, *breakdown["delta"].tolist(), total_cur]
            bridge = go.Figure(
                go.Waterfall(
                    x=labels,
                    y=values,
                    measure=measures,
                    increasing={"marker": {"color": SEMANTIC["increase"]}},
                    decreasing={"marker": {"color": SEMANTIC["decrease"]}},
                    totals={"marker": {"color": SEMANTIC["paid"]}},
                    connector={"line": {"color": "#e2e8f0"}},
                )
            )
            plotly(style_fig(bridge, has_legend=False), key="home_bridge")
        with movers_tab:
            movers = cross_provider_movers(month, prior)
            if movers.empty:
                st.info("No month-over-month movers in this range.")
            else:
                section_caption(f"Largest absolute changes · {month:%b %Y} vs {prior:%b %Y}")
                html_table(
                    movers,
                    money_cols=["cost_delta"],
                    pct_cols=["cost_pct_change"],
                    rename={
                        "provider": "Provider",
                        "type": "Type",
                        "driver": "Driver",
                        "cost_delta": "Δ vs prior",
                        "cost_pct_change": "MoM %",
                    },
                )

    if attr and tco_page is not None:
        total_tco, unattr = attr
        with panel(tone="amber"):
            section_title("Databricks total cost")
            section_caption(
                f"Databricks DBU plus AWS infrastructure where we can attribute it. "
                f"{month:%b %Y} TCO: **{compact_money(total_tco)}**"
                + (
                    f" · {compact_money(unattr)} AWS not yet mapped to a cluster"
                    if unattr
                    else ""
                )
                + "."
            )
            st.page_link(tco_page, label="See total cost details →", icon="🧮")

    with panel(tone="default"):
        section_title("Provider details")
        cols = st.columns(min(len(rows), 4), gap="medium")
        for col, row in zip(cols, breakdown.itertuples(), strict=False):
            with col:
                delta = float(row.delta)
                arrow = "↑" if delta >= 0 else "↓"
                color = provider_color(label=str(row.provider), group=str(row.group))
                delta_hex = (
                    color if delta == 0 else SEMANTIC["increase" if delta > 0 else "decrease"]
                )
                delta_text = (
                    f"{arrow} {compact_money(abs(delta))} vs {prior:%b %Y}"
                    if delta
                    else f"Flat vs {prior:%b %Y}"
                )
                page = (provider_pages or {}).get(str(row.group))
                provider_card(
                    name=str(row.provider),
                    amount=compact_money(float(row.net_cost)),
                    delta_text=delta_text,
                    color=color,
                    delta_color=delta_hex,
                    page=page,
                    link_label=f"Open {row.provider} →" if page else None,
                )
