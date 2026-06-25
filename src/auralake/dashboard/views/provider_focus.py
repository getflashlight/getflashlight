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


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


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
    if group == "databricks":  # prototype: click-to-drill, Databricks only for now
        _monthly_drill(group, label, end, sm)
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


def _selected_month(event: Any) -> str | None:
    """Pull the clicked month label ('YYYY-MM') out of a Plotly selection event."""
    try:
        points = event["selection"]["points"]
    except (TypeError, KeyError, IndexError):
        return None
    return str(points[-1]["x"]) if points else None


def _monthly_drill(group: str, label: str, end: date, sm: date) -> None:
    """Clickable monthly-spend bars → per-SKU breakdown of *why* a month moved.

    Click a bar and the panel below decomposes that month's change vs the prior month
    into volume (more usage) vs rate (price/mix) using ``sku_month_over_month`` — the
    one view that already carries that split, which is why this prototypes on
    Databricks (its SKU grain makes the breakdown meaningful).
    """
    bills = gold_df(
        f'SELECT charge_month, net_cost FROM "{group}".monthly_bill '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' ORDER BY charge_month"
    )
    if bills.empty:
        return
    bills["month"] = pd.to_datetime(bills["charge_month"]).dt.strftime("%Y-%m")
    fig = px.bar(bills, x="month", y="net_cost", labels={"month": "", "net_cost": ""})
    fig.update_traces(marker_color=PALETTE[1])
    event = plotly(
        style_fig(fig),
        title=f"Monthly {label} net cost — click a bar to drill in",
        key=f"{group}_monthly_drill",
        on_select=True,
    )

    picked = _selected_month(event)
    if picked is None:
        st.caption("Tip: click a month above to see which SKUs drove its change.")
        return
    _drilldown(group, picked)


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
    st.divider()
    st.markdown(f"##### Why did {m:%b %Y} change?")
    sign = "↑" if delta >= 0 else "↓"
    st.caption(
        f"Net cost {compact_money(net)} · {sign} {compact_money(abs(delta))} vs {prior:%b %Y}. "
        "Volume = usage changed; Rate = price/mix changed (the two sum to the change)."
    )
    def _signed(v: float) -> str:
        return f"{'+' if v >= 0 else '−'}{compact_money(abs(v))}"

    kpi_cards(
        [
            (f"{m:%b %Y} net", compact_money(net), "after credits"),
            ("Change vs prior", _signed(delta), f"vs {prior:%b}"),
            ("Volume effect", _signed(vol), "usage"),
            ("Rate effect", _signed(rate), "price / mix"),
        ],
        key=f"{group}_drill",
    )

    movers = gold_df(
        "SELECT sku_id, net_cost, cost_delta, volume_effect, rate_effect, cost_pct_change "
        f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{m.date()}\' '
        "AND cost_delta IS NOT NULL ORDER BY abs(cost_delta) DESC LIMIT 15"
    )
    if movers.empty:
        return
    disp = movers.copy()  # keep movers["sku_id"] raw — it's the query key for the SKU drill
    disp["sku_id"] = disp["sku_id"].str.replace("ENTERPRISE_", "", regex=False)
    st.markdown("###### Top movers — SKUs driving the change")
    heatmap_table(
        disp,
        heat_col="cost_pct_change",
        money_cols=["net_cost", "cost_delta", "volume_effect", "rate_effect"],
        rename={
            "sku_id": "SKU",
            "net_cost": "Net cost",
            "cost_delta": "Δ vs prior",
            "volume_effect": "Volume Δ",
            "rate_effect": "Rate Δ",
            "cost_pct_change": "MoM %",
        },
    )
    _sku_detail(group, m, prior, movers["sku_id"].tolist())


def _cur_prev(m: pd.Timestamp, prior: pd.Timestamp, col: str = "net_cost", sfx: str = "") -> str:
    """SQL for two FILTERed sums of ``col`` aliased ``cur{sfx}`` (m) / ``prev{sfx}`` (prior)."""
    return (
        f"coalesce(sum({col}) FILTER (WHERE charge_month='{m.date()}'),0) AS cur{sfx}, "
        f"coalesce(sum({col}) FILTER (WHERE charge_month='{prior.date()}'),0) AS prev{sfx}"
    )


def _sku_detail(group: str, m: pd.Timestamp, prior: pd.Timestamp, skus: list[str]) -> None:
    """Drill one moved SKU into *where* it moved (resource/workspace) and *who* owns it (tag).

    Answers the follow-ups a SKU-level delta can't: which exact resource (e.g. a SQL
    warehouse) and workspace drove it, how much more it was *used*, and whether the
    change lands on a project/team tag — with the unattributed remainder surfaced.
    """
    if not skus:
        return
    st.markdown("###### Drill into a SKU — where exactly did it move?")
    raw = st.selectbox(
        "SKU", options=skus, format_func=lambda s: s.replace("ENTERPRISE_", ""),
        key=f"{group}_drill_sku", label_visibility="collapsed",
    )
    sku, sku_label = _q(raw), raw.replace("ENTERPRISE_", "")
    months = f"charge_month IN ('{m.date()}','{prior.date()}')"

    _resource_breakdown(group, sku, sku_label, m, prior, months)
    _tag_breakdown(group, sku, m, prior, months)


def _resource_breakdown(
    group: str, sku: str, sku_label: str, m: pd.Timestamp, prior: pd.Timestamp, months: str
) -> None:
    """Per-resource (warehouse/job/cluster) + workspace movement for the chosen SKU."""
    res = gold_df(
        "SELECT resource_name, resource_type, sub_account_id, "
        f"{_cur_prev(m, prior)}, {_cur_prev(m, prior, 'consumed_quantity', '_q')}, "
        "max(consumed_unit) AS unit "
        f'FROM "{group}".resource_month '
        f"WHERE sku_id='{sku}' AND {months} "
        "GROUP BY resource_name, resource_type, sub_account_id"
    )
    if res.empty:
        return
    res["delta"] = res["cur"] - res["prev"]
    res["qty_delta"] = res["cur_q"] - res["prev_q"]
    res["pct"] = res.apply(lambda r: 100 * r["delta"] / r["prev"] if r["prev"] else None, axis=1)
    res = res.loc[res["delta"].abs().sort_values(ascending=False).index].head(12)
    unit = next(iter(res["unit"].dropna()), "units")
    st.caption(
        f"Resources moving **{sku_label}** · {prior:%b}→{m:%b}. "
        f"Usage Δ is in {unit} (this billing data carries no query/operation counts)."
    )
    cols = ["resource_name", "resource_type", "sub_account_id", "cur", "delta", "qty_delta", "pct"]
    html_table(
        res[cols],
        money_cols=["cur", "delta"], num_cols=["qty_delta"], pct_cols=["pct"],
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
