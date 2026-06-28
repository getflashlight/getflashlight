"""Per-provider FOCUS spend — one page per provider, rendered from its GOLD group.

Each provider's GOLD lives in its own group schema (``aws.*``, ``databricks.*``, …),
so the page reads ``<group>.<view>`` with no ``provider_name`` filter. A single
sidebar **from→to date range** drives every panel below it. ``render`` is bound per
provider in ``app.py``; widget keys are namespaced by group so two provider pages
don't collide in session state.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.context import global_range, range_has_partial_month
from auralake.dashboard.data import NO_DATA_MSG, gold_df, has_data
from auralake.dashboard.data import to_date as _d
from auralake.dashboard.summary import _service_movers, driver_dim, provider_spend_summary
from auralake.dashboard.theme import (
    SEMANTIC,
    compact_money,
    delta_variant,
    filterable_table,
    heatmap_table,
    html_table,
    kpi_cards,
    panel,
    plotly,
    provider_color,
    rgba_hex,
    section_caption,
    section_subtitle,
    section_title,
    style_fig,
)


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def render(group: str, label: str) -> None:
    """Render the FOCUS spend page for one provider group (``group`` schema, ``label`` UI)."""
    st.title(f"{label} spend")
    if not has_data():
        st.info(NO_DATA_MSG)
        return

    bounds = gold_df(
        f'SELECT min(charge_day) AS lo, max(charge_day) AS hi FROM "{group}".spend_trend_daily'
    )
    if bounds.empty or pd.isna(bounds["lo"].iloc[0]):
        st.info(f"No {label} billing data found. Your admin may need to enable the connection.")
        return

    lo, hi = _d(bounds["lo"].iloc[0]), _d(bounds["hi"].iloc[0])
    start, end = global_range()
    start, end = max(start, lo), min(end, hi)
    sm = start.replace(day=1)
    accent = provider_color(label=label, group=group)
    partial = range_has_partial_month(end)
    cap = f"{label} net cost over the chosen window and what's driving it."
    if partial:
        cap += " Partial month — current month is still accruing."
    st.caption(cap)

    _kpis(group, label, start, end, sm, accent=accent, partial=partial)
    st.markdown(provider_spend_summary(group, label, start, end, partial=partial))

    tab_trend, tab_breakdown, tab_tags = st.tabs(
        ["Trend & changes", "Breakdown", "Tags"],
    )
    with tab_trend:
        with panel(tone="teal", flush=True):
            _trend(group, label, start, end, accent=accent)
            _monthly_drill(group, label, end, sm, accent=accent)
    with tab_breakdown:
        with panel(tone="default", flush=True):
            _spend_pivot(group, end, sm)
            _savings(group, label, end, sm)
            _driver_mom(group, end)
    with tab_tags:
        with panel(tone="default", flush=True):
            _tags(group, label, end, sm)


def _kpis(
    group: str, label: str, start: date, end: date, sm: date, *, accent: str, partial: bool
) -> None:
    agg = gold_df(
        "SELECT coalesce(sum(net_cost),0) AS net, coalesce(sum(list_cost),0) AS lst, "
        f'coalesce(sum(savings),0) AS sav FROM "{group}".monthly_bill '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    ).iloc[0]
    net, lst, sav = float(agg["net"]), float(agg["lst"]), float(agg["sav"])
    disc = f"{100 * sav / lst:.1f}%" if lst else "—"
    span = f"{start:%b %d} → {end:%b %d}" + (" · partial" if partial else "")
    kpi_cards(
        [
            (f"{label} net", compact_money(net), span, "default"),
            (f"{label} list", compact_money(lst), "before discounts", "paid"),
            (f"{label} savings", compact_money(sav), "vs list", "savings"),
            ("Realized discount", disc, "off list", "savings"),
        ],
        key=group,
        accent=accent,
        partial=partial,
    )


def _trend(group: str, label: str, start: date, end: date, *, accent: str) -> None:
    trend = gold_df(
        f'SELECT charge_day, net_cost FROM "{group}".spend_trend_daily '
        f"WHERE charge_day >= '{start}' AND charge_day <= '{end}' ORDER BY charge_day"
    )
    if trend.empty:
        st.info(f"No daily {label} rows in range.")
        return
    fig = px.area(trend, x="charge_day", y="net_cost", labels={"charge_day": "", "net_cost": ""})
    fig.update_traces(line_color=accent, fillcolor=rgba_hex(accent, 0.18))
    section_title("Daily spend", flush=True)
    plotly(style_fig(fig, has_legend=False), key=f"{group}_trend")


def _selected_month(event: Any) -> str | None:
    """Pull the clicked month label ('YYYY-MM') out of a Plotly selection event."""
    try:
        points = event["selection"]["points"]
    except (TypeError, KeyError, IndexError):
        return None
    return str(points[-1]["x"]) if points else None


def _monthly_drill(group: str, label: str, end: date, sm: date, *, accent: str) -> None:
    """Clickable monthly bars → per-SKU or per-service breakdown of *why* a month moved."""
    id_col, id_label, _ = driver_dim(group)
    bills = gold_df(
        f'SELECT charge_month, net_cost FROM "{group}".monthly_bill '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' ORDER BY charge_month"
    )
    if bills.empty:
        return
    bills["month"] = pd.to_datetime(bills["charge_month"]).dt.strftime("%Y-%m")
    fig = px.bar(bills, x="month", y="net_cost", labels={"month": "", "net_cost": ""})
    fig.update_traces(marker_color=accent)
    section_title("Monthly net cost — click a bar to drill in")
    section_caption(f"Tip: select a month to see which {id_label.lower()}s drove its change.")
    event = plotly(
        style_fig(fig, has_legend=False),
        key=f"{group}_monthly_drill",
        on_select=True,
    )

    picked = _selected_month(event)
    if picked is None:
        return
    _drilldown(group, picked)


def _drill_movers(group: str, month: date, prior: date) -> pd.DataFrame:
    id_col, _, _ = driver_dim(group)
    if group == "databricks":
        return gold_df(
            f"SELECT {id_col}, net_cost, cost_delta, volume_effect, rate_effect, cost_pct_change "
            f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{month}\' '
            "AND cost_delta IS NOT NULL ORDER BY abs(cost_delta) DESC LIMIT 15"
        )
    svc = _service_movers(group, month, prior).head(15)
    return svc.rename(columns={"k": id_col})


def _drilldown(group: str, month_label: str) -> None:
    """Render the month-over-month breakdown for the clicked month."""
    m = pd.Timestamp(f"{month_label}-01")
    prior = m - pd.DateOffset(months=1)

    agg = gold_df(
        "SELECT coalesce(sum(net_cost),0) AS net, coalesce(sum(cost_delta),0) AS delta, "
        "coalesce(sum(volume_effect),0) AS vol, coalesce(sum(rate_effect),0) AS rate, "
        "count(prev_cost) AS comparable "
        f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{m.date()}\''
    ).iloc[0]
    if int(agg["comparable"]) == 0:
        st.divider()
        st.info(f"No prior month to compare {m:%b %Y} against — it's the earliest in the data.")
        return

    net, delta, vol, rate = (float(agg[k]) for k in ("net", "delta", "vol", "rate"))
    sign = "↑" if delta >= 0 else "↓"
    section_subtitle(f"Why did {m:%b %Y} change?")
    section_caption(
        f"Net cost {compact_money(net)} · {sign} {compact_money(abs(delta))} vs {prior:%b %Y}. "
        "Volume = usage changed; Rate = price/mix changed (the two sum to the change)."
    )
    def _signed(v: float) -> str:
        return f"{'+' if v >= 0 else '−'}{compact_money(abs(v))}"

    kpi_cards(
        [
            (f"{m:%b %Y} net", compact_money(net), "after credits", "default"),
            ("Change vs prior", _signed(delta), f"vs {prior:%b}", delta_variant(delta)),
            ("Volume effect", _signed(vol), "usage", "volume"),
            ("Rate effect", _signed(rate), "price / mix", "rate"),
        ],
        key=f"{group}_drill",
    )

    effects = pd.DataFrame(
        [{"component": "Volume", "effect": vol}, {"component": "Rate", "effect": rate}]
    )
    fig = px.bar(
        effects,
        x="component",
        y="effect",
        color="component",
        color_discrete_map={"Volume": SEMANTIC["volume"], "Rate": SEMANTIC["rate"]},
        labels={"component": "", "effect": ""},
    )
    fig.update_layout(showlegend=False)
    plotly(
        style_fig(fig, has_legend=False),
        title=f"What drove the change · {m:%b %Y}",
        key=f"{group}_drill_effects",
    )

    movers = _drill_movers(group, m.date(), prior.date())
    if movers.empty:
        return
    id_col, id_label, _ = driver_dim(group)
    disp = movers.copy()
    if group == "databricks":
        disp[id_col] = disp[id_col].str.replace("ENTERPRISE_", "", regex=False)
        section_subtitle(f"Top movers — {id_label}s driving the change")
        heatmap_table(
            disp,
            heat_col="cost_pct_change",
            money_cols=["net_cost", "cost_delta", "volume_effect", "rate_effect"],
            rename={
                id_col: id_label,
                "net_cost": "Net cost",
                "cost_delta": "Δ vs prior",
                "volume_effect": "Volume Δ",
                "rate_effect": "Rate Δ",
                "cost_pct_change": "MoM %",
            },
        )
    else:
        section_subtitle(f"Top movers — {id_label}s driving the change")
        filterable_table(
            disp[[id_col, "net_cost", "cost_delta", "cost_pct_change"]],
            filter_col=id_col,
            file_name=f"{group}_movers.csv",
            key=f"{group}_drill_movers",
            money_cols=["net_cost", "cost_delta"],
            pct_cols=["cost_pct_change"],
            rename={
                id_col: id_label,
                "net_cost": "Net cost",
                "cost_delta": "Δ vs prior",
                "cost_pct_change": "MoM %",
            },
        )
    _driver_detail(group, m, prior, movers[id_col].tolist())


def _cur_prev(m: pd.Timestamp, prior: pd.Timestamp, col: str = "net_cost", sfx: str = "") -> str:
    """SQL for two FILTERed sums of ``col`` aliased ``cur{sfx}`` (m) / ``prev{sfx}`` (prior)."""
    return (
        f"coalesce(sum({col}) FILTER (WHERE charge_month='{m.date()}'),0) AS cur{sfx}, "
        f"coalesce(sum({col}) FILTER (WHERE charge_month='{prior.date()}'),0) AS prev{sfx}"
    )


def _driver_detail(group: str, m: pd.Timestamp, prior: pd.Timestamp, drivers: list[str]) -> None:
    """Drill a moved SKU or service into resources (and tags for SKU grain)."""
    if not drivers:
        return
    id_col, id_label, _ = driver_dim(group)
    section_subtitle(f"Drill into a {id_label} — where exactly did it move?")
    if group == "databricks":
        raw = st.selectbox(
            id_label, options=drivers, format_func=lambda s: s.replace("ENTERPRISE_", ""),
            key=f"{group}_drill_driver", label_visibility="collapsed",
        )
        key_val, label = _q(raw), raw.replace("ENTERPRISE_", "")
        months = f"charge_month IN ('{m.date()}','{prior.date()}')"
        _resource_breakdown(group, "sku_id", key_val, label, m, prior, months)
        _tag_breakdown(group, key_val, m, prior, months)
    else:
        service = st.selectbox(
            id_label, options=drivers, key=f"{group}_drill_driver", label_visibility="collapsed",
        )
        months = f"charge_month IN ('{m.date()}','{prior.date()}')"
        _resource_breakdown(group, "service_name", _q(service), service, m, prior, months)
        section_caption("Team/project tags are on the Tags tab — they span all services.")


def _resource_breakdown(
    group: str,
    filter_col: str,
    key_val: str,
    label: str,
    m: pd.Timestamp,
    prior: pd.Timestamp,
    months: str,
) -> None:
    """Per-resource movement for the chosen SKU or service."""
    res = gold_df(
        "SELECT resource_name, resource_type, sub_account_id, "
        f"{_cur_prev(m, prior)}, {_cur_prev(m, prior, 'consumed_quantity', '_q')}, "
        "max(consumed_unit) AS unit "
        f'FROM "{group}".resource_month '
        f"WHERE {filter_col}='{key_val}' AND {months} "
        "GROUP BY resource_name, resource_type, sub_account_id"
    )
    if res.empty:
        return
    res["delta"] = res["cur"] - res["prev"]
    res["qty_delta"] = res["cur_q"] - res["prev_q"]
    res["pct"] = res.apply(lambda r: 100 * r["delta"] / r["prev"] if r["prev"] else None, axis=1)
    res = res.loc[res["delta"].abs().sort_values(ascending=False).index].head(12)
    unit = next(iter(res["unit"].dropna()), "units")
    cols = ["resource_name", "resource_type", "sub_account_id", "cur", "delta", "qty_delta", "pct"]
    st.caption(
        f"Resources moving **{label}** · {prior:%b}→{m:%b}. "
        f"Usage Δ is in {unit} (this billing data carries no query/operation counts)."
    )
    filterable_table(
        res[cols],
        filter_col="resource_name",
        file_name=f"{group}_resources.csv",
        key=f"{group}_drill_res",
        money_cols=["cur", "delta"],
        num_cols=["qty_delta"],
        pct_cols=["pct"],
        rename={
            "resource_name": "Resource", "resource_type": "Type", "sub_account_id": "Workspace",
            "cur": f"{m:%b} net", "delta": "Δ vs prior", "qty_delta": f"Usage Δ ({unit})",
            "pct": "MoM %",
        },
    )


def _tag_breakdown(
    group: str, sku: str, m: pd.Timestamp, prior: pd.Timestamp, months: str
) -> None:
    """Attribute the chosen SKU's spend to a project/team tag, surfacing the untagged remainder."""
    keys = gold_df(
        f'SELECT tag_key, sum(net_cost) AS net FROM "{group}".spend_by_sku_tag_month '
        f"WHERE sku_id='{sku}' GROUP BY tag_key ORDER BY net DESC"
    )
    if keys.empty:
        st.caption("No cost-allocation tags on this SKU's spend.")
        return
    opts = keys["tag_key"].tolist()
    default = next((k for k in ("project", "team", "owner", "env") if k in opts), opts[0])
    tk = st.selectbox(
        "Attribution tag", options=opts, index=opts.index(default),
        key=f"{group}_drill_tagkey", label_visibility="collapsed",
    )
    tv = gold_df(
        f"SELECT coalesce(tag_value,'(none)') AS tag_value, {_cur_prev(m, prior)} "
        f'FROM "{group}".spend_by_sku_tag_month '
        f"WHERE sku_id='{sku}' AND tag_key='{_q(tk)}' AND {months} GROUP BY tag_value"
    )
    tot = gold_df(
        f'SELECT {_cur_prev(m, prior)} FROM "{group}".spend_by_sku_month '
        f"WHERE sku_id='{sku}' AND {months}"
    ).iloc[0]
    # Attribution honesty: untagged spend is the SKU total minus what this tag accounts for.
    unattr_cur = float(tot["cur"]) - float(tv["cur"].sum())
    unattr_prev = float(tot["prev"]) - float(tv["prev"].sum())
    if round(unattr_cur, 2) or round(unattr_prev, 2):
        unattr = pd.DataFrame(
            [{"tag_value": "(unattributed)", "cur": unattr_cur, "prev": unattr_prev}]
        )
        tv = pd.concat([tv, unattr], ignore_index=True)
    tv["delta"] = tv["cur"] - tv["prev"]
    tv = tv.loc[tv["cur"].abs().sort_values(ascending=False).index].head(12)
    st.caption(f"Attributed by the `{tk}` tag · {prior:%b}→{m:%b}")
    html_table(
        tv[["tag_value", "cur", "delta"]],
        money_cols=["cur", "delta"],
        rename={"tag_value": tk, "cur": f"{m:%b} net", "delta": "Δ vs prior"},
    )


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

    section_title(f"{label}s × month — spend")
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
    out = pivot.reset_index()
    money_cols = [c for c in out.columns if c != "k"]
    filterable_table(
        out,
        filter_col="k",
        file_name=f"{group}_spend_pivot.csv",
        key=f"{group}_pivot",
        money_cols=money_cols,
        rename={"k": label},
        compact=True,
    )


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
    section_title("Bill: list vs effective")
    section_caption(
        f"Effective + savings = list. Latest month · {latest['savings_pct']:.1f}% off list."
    )
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
        color_discrete_map={
            "Effective (paid)": SEMANTIC["paid"],
            "Savings (discount)": SEMANTIC["savings"],
        },
        category_orders={"component": ["Effective (paid)", "Savings (discount)"]},
        labels={"month": "", "cost": "", "component": ""},
    )
    plotly(style_fig(fig, has_legend=True), key=f"{group}_savings")


def _driver_mom(group: str, end: date) -> None:
    id_col, id_label, _ = driver_dim(group)
    section_title(f"{id_label} month-over-month")
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
    section_caption(f"Top {id_label.lower()}s by net cost · {cmp_month:%b %Y} vs {prior:%b %Y}")
    if group == "databricks":
        mom = gold_df(
            f"SELECT {id_col}, net_cost, cost_delta, cost_pct_change "
            f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{cmp_month.date()}\' '
            "ORDER BY net_cost DESC LIMIT 20"
        )
        if mom.empty:
            st.info(f"No {id_label.lower()} movement rows for the latest complete month.")
            return
        heatmap_table(
            mom,
            heat_col="cost_pct_change",
            money_cols=["net_cost", "cost_delta"],
            rename={
                id_col: id_label,
                "net_cost": "Net cost",
                "cost_delta": "Δ vs prior",
                "cost_pct_change": "MoM %",
            },
        )
        return
    svc = _service_movers(group, cmp_month.date(), prior.date()).head(20)
    if svc.empty:
        st.info(f"No {id_label.lower()} movement rows for the latest complete month.")
        return
    filterable_table(
        svc.rename(columns={"k": id_col}),
        filter_col=id_col,
        file_name=f"{group}_mom.csv",
        key=f"{group}_mom",
        money_cols=["net_cost", "cost_delta"],
        pct_cols=["cost_pct_change"],
        rename={
            id_col: id_label,
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
        section_title("Spend by tag")
        st.info("No tagged spend in range.")
        return

    section_title("Spend by tag")
    options = keys["tag_key"].tolist()
    default = "team" if "team" in options else options[0]
    sel = st.selectbox(
        "Tag key",
        options=options,
        index=options.index(default),
        key=f"{group}_tagkey",
        label_visibility="collapsed",
    )
    section_caption(f"{label} spend broken down by the `{sel}` tag")
    tags = gold_df(
        f"SELECT tag_value, sum(net_cost) AS net_cost FROM \"{group}\".spend_by_tag_month "
        f"WHERE tag_key = '{sel}' AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY tag_value ORDER BY net_cost DESC LIMIT 20"
    )
    if tags.empty:
        st.info("No values for this tag in range.")
        return
    filterable_table(
        tags,
        filter_col="tag_value",
        file_name=f"{group}_tags.csv",
        key=f"{group}_tags",
        money_cols=["net_cost"],
        rename={"tag_value": sel, "net_cost": "Net cost"},
    )
