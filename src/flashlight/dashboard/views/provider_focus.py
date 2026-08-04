"""Per-provider FOCUS spend — one page per provider, rendered from its GOLD group.

Each provider's GOLD lives in its own group schema (``aws.*``, ``databricks.*``, …),
so the page reads ``<group>.<view>`` with no ``provider_name`` filter. A per-page
date range picker (own state, not shared across page navigations — see
``router.py``) drives every panel below it. ``render`` is bound per provider by
``router.build_pages()``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date

import pandas as pd
import plotly.express as px
from nicegui import ui
from nicegui.events import GenericEventArguments

from flashlight.dashboard import chrome, router
from flashlight.dashboard.chrome import DateState
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.summary import _service_movers, driver_dim, provider_spend_summary
from flashlight.dashboard.theme import compact_money, delta_variant, provider_color, rgba_hex


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _info(text: str) -> None:
    ui.label(text).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


def render(
    group: str,
    label: str,
    *,
    extra_tabs: Sequence[tuple[str, Callable[[], None]]] = (),
) -> None:
    bounds = gold_df(
        f'SELECT min(charge_day) AS lo, max(charge_day) AS hi FROM "{group}".spend_trend_daily'
    )
    if bounds.empty or pd.isna(bounds["lo"].iloc[0]):
        _info(f"No {label} billing data found. Your admin may need to enable the connection.")
        return

    lo, hi = _d(bounds["lo"].iloc[0]), _d(bounds["hi"].iloc[0])
    date_state: DateState = {
        "start": max(lo, chrome.months_back(hi, 6)),
        "end": hi,
        "bounds_min": lo,
        "bounds_max": hi,
    }
    accent = provider_color(label=label, group=group)

    with ui.row().classes("items-center justify-between w-full"):
        chrome.section_title(f"{label} spend")
        chrome.date_range_control(date_state, lambda: body.refresh())

    @ui.refreshable
    def body() -> None:
        start, end = date_state["start"], date_state["end"]
        sm = start.replace(day=1)
        partial = router.range_has_partial_month(end)
        cap = f"{label} net cost over the chosen window and what's driving it."
        if partial:
            cap += " Partial month — current month is still accruing."
        chrome.section_caption(cap)

        _kpis(group, label, start, end, sm, accent=accent, partial=partial)
        ui.markdown(provider_spend_summary(group, label, start, end, partial=partial)).style(
            f"color:{chrome.INK_SECONDARY};font-size:13px;"
        )

        with ui.tabs().classes("w-full") as tabs:
            tab_trend = ui.tab("Trend & changes")
            tab_breakdown = ui.tab("Breakdown")
            tab_tags = ui.tab("Tags")
            extra_refs = [ui.tab(title) for title, _ in extra_tabs]
        with ui.tab_panels(tabs, value=tab_trend).classes("w-full").style(
            "background:transparent;"
        ):
            with ui.tab_panel(tab_trend), chrome.panel():
                _trend(group, label, start, end, accent=accent)
                _monthly_drill(group, label, end, sm, accent=accent)
            with ui.tab_panel(tab_breakdown), chrome.panel():
                _spend_pivot(group, end, sm)
                _cost_subcategory(group, end, sm)
                _savings(group, label, end, sm)
                _commitment(group, end, sm)
                _driver_mom(group, end)
            with ui.tab_panel(tab_tags), chrome.panel():
                _tags(group, label, end, sm)
            # extra_tabs already draw their own section titles/panels internally,
            # so no chrome.panel() wrapper here (matches how redshift_focus.render()
            # wasn't double-wrapped when it was nested under the old AWS tabs).
            for tab_ref, (_, render_fn) in zip(extra_refs, extra_tabs, strict=True):
                with ui.tab_panel(tab_ref):
                    render_fn()

    body()


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
    cards: list[chrome.KpiCard] = [
        (f"{label} net", compact_money(net), span),
        (f"{label} list", compact_money(lst), "before discounts", "paid"),
        (f"{label} savings", compact_money(sav), "vs list", "savings"),
        ("Realized discount", disc, "off list", "savings"),
    ]
    projected = _projected_this_month(group)
    if projected is not None:
        cards.append(projected)
    chrome.kpi_row(cards)


def _projected_this_month(group: str) -> tuple[str, str, str] | None:
    """Where the latest month lands at its current run rate, or None if unknowable.

    Only the run_rate row is shown here — it's the number that's actionable mid-month.
    The 3-month trend rows live in the forecast view for whoever wants them.

    Absent from a lake published before the forecast view existed, so the file is
    checked rather than the query being wrapped in a bare except — a real SQL error
    should still surface loudly.
    """
    if not gold_view_published(group, "spend_forecast_month"):
        return None
    row = gold_df(
        "SELECT charge_month, forecast_cost, actual_to_date, history_days "
        f'FROM "{group}".spend_forecast_month '
        "WHERE forecast_kind = 'run_rate' ORDER BY charge_month DESC LIMIT 1"
    )
    if row.empty or row["forecast_cost"].iloc[0] is None:
        return None
    days = int(row["history_days"].iloc[0])
    month = pd.Timestamp(row["charge_month"].iloc[0])
    return (
        "Projected",
        compact_money(float(row["forecast_cost"].iloc[0])),
        f"{month:%b %Y} at {days}-day run rate",
    )


def _trend(group: str, label: str, start: date, end: date, *, accent: str) -> None:
    trend = gold_df(
        f'SELECT charge_day, net_cost FROM "{group}".spend_trend_daily '
        f"WHERE charge_day >= '{start}' AND charge_day <= '{end}' ORDER BY charge_day"
    )
    if trend.empty:
        _info(f"No daily {label} rows in range.")
        return
    chrome.panel_title("Daily spend")
    fig = px.area(trend, x="charge_day", y="net_cost", labels={"charge_day": "", "net_cost": ""})
    fig.update_traces(line_color=accent, fillcolor=rgba_hex(accent, 0.18))
    chrome.plot(chrome.style_fig(fig, has_legend=False))


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
    chrome.panel_title("Monthly net cost — click a bar to drill in")
    chrome.section_caption(
        f"Tip: select a month to see which {id_label.lower()}s drove its change."
    )
    fig = px.bar(
        bills, x="month", y="net_cost", custom_data=["month"], labels={"month": "", "net_cost": ""}
    )
    fig.update_traces(marker_color=accent)
    chart = chrome.plot(chrome.style_fig(fig, has_legend=False, category_x=True))

    drill_container = ui.column().classes("w-full gap-4")

    @ui.refreshable
    def drill_body(picked: str | None) -> None:
        drill_container.clear()
        if picked is None:
            return
        with drill_container:
            _drilldown(group, picked)

    def _on_click(e: GenericEventArguments) -> None:
        points = e.args.get("points") or []
        if points:
            drill_body.refresh(str(points[0]["customdata"][0]))

    chart.on("plotly_click", _on_click)


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
        _info(f"No prior month to compare {m:%b %Y} against — it's the earliest in the data.")
        return

    net, delta, vol, rate = (float(agg[k]) for k in ("net", "delta", "vol", "rate"))
    sign = "↑" if delta >= 0 else "↓"
    chrome.panel_title(f"Why did {m:%b %Y} change?")
    chrome.section_caption(
        f"Net cost {compact_money(net)} · {sign} {compact_money(abs(delta))} vs {prior:%b %Y}. "
        "Volume = usage changed; Rate = price/mix changed (the two sum to the change)."
    )

    def _signed(v: float) -> str:
        return f"{'+' if v >= 0 else '−'}{compact_money(abs(v))}"

    chrome.kpi_row(
        [
            (f"{m:%b %Y} net", compact_money(net), "after credits"),
            ("Change vs prior", _signed(delta), f"vs {prior:%b}", delta_variant(delta)),
            ("Volume effect", _signed(vol), "usage", "volume"),
            ("Rate effect", _signed(rate), "price / mix", "rate"),
        ],
    )

    effects = pd.DataFrame(
        [{"component": "Volume", "effect": vol}, {"component": "Rate", "effect": rate}]
    )
    fig = px.bar(
        effects,
        x="component",
        y="effect",
        color="component",
        color_discrete_map={"Volume": chrome.SEMANTIC["volume"], "Rate": chrome.SEMANTIC["rate"]},
        labels={"component": "", "effect": ""},
    )
    fig.update_layout(showlegend=False)
    chrome.panel_title(f"What drove the change · {m:%b %Y}")
    chrome.plot(chrome.style_fig(fig, has_legend=False))

    movers = _drill_movers(group, m.date(), prior.date())
    if movers.empty:
        return
    id_col, id_label, _ = driver_dim(group)
    disp = movers.copy()
    if group == "databricks":
        disp[id_col] = disp[id_col].str.replace("ENTERPRISE_", "", regex=False)
        chrome.panel_title(f"Top movers — {id_label}s driving the change")
        chrome.heatmap_table(
            disp,
            heat_col="cost_pct_change",
            key=f"{group}_drill_movers",
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
        chrome.panel_title(f"Top movers — {id_label}s driving the change")
        chrome.searchable_table(
            disp[[id_col, "net_cost", "cost_delta", "cost_pct_change"]],
            key=f"{group}_drill_movers",
            search_col=id_col,
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
    chrome.panel_title(f"Drill into a {id_label} — where exactly did it move?")
    months = f"charge_month IN ('{m.date()}','{prior.date()}')"

    def _fmt(raw: str) -> str:
        return raw.replace("ENTERPRISE_", "") if group == "databricks" else raw

    body_container = ui.column().classes("w-full gap-4")

    @ui.refreshable
    def _detail_body(selected: str) -> None:
        body_container.clear()
        with body_container:
            if group == "databricks":
                key_val, drv_label = _q(selected), _fmt(selected)
                _resource_breakdown(group, "sku_id", key_val, drv_label, m, prior, months)
                _tag_breakdown(group, key_val, m, prior, months)
            else:
                _resource_breakdown(group, "service_name", _q(selected), selected, m, prior, months)
                chrome.section_caption(
                    "Team/project tags are on the Tags tab — they span all services."
                )

    ui.select(
        options={d: _fmt(d) for d in drivers},
        value=drivers[0],
        on_change=lambda e: _detail_body.refresh(e.value),
    ).props("dense outlined").classes("w-64").style(f"color:{chrome.INK_PRIMARY}")
    _detail_body(drivers[0])


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
    chrome.section_caption(
        f"Resources moving {label} · {prior:%b}→{m:%b}. "
        f"Usage Δ is in {unit} (this billing data carries no query/operation counts)."
    )
    chrome.searchable_table(
        res[cols],
        key=f"{group}_drill_res",
        search_col="resource_name",
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
        chrome.section_caption("No cost-allocation tags on this SKU's spend.")
        return
    opts = keys["tag_key"].tolist()
    default = next((k for k in ("project", "team", "owner", "env") if k in opts), opts[0])

    body_container = ui.column().classes("w-full gap-4")

    @ui.refreshable
    def _tag_body(tk: str) -> None:
        body_container.clear()
        with body_container:
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
            chrome.section_caption(f"Attributed by the `{tk}` tag · {prior:%b}→{m:%b}")
            chrome.flat_table(
                tv[["tag_value", "cur", "delta"]],
                key=f"{group}_tag_breakdown",
                money_cols=["cur", "delta"],
                rename={"tag_value": tk, "cur": f"{m:%b} net", "delta": "Δ vs prior"},
            )

    ui.select(options=opts, value=default, on_change=lambda e: _tag_body.refresh(e.value)).props(
        "dense outlined"
    ).classes("w-48").style(f"color:{chrome.INK_PRIMARY}")
    _tag_body(default)


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

    chrome.panel_title(f"{label}s × month — spend")
    df = gold_df(
        f"SELECT {dim} AS k, charge_month, sum(net_cost) AS net_cost "
        f'FROM "{group}".{view} '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        f"GROUP BY {dim}, charge_month"
    )
    if df.empty:
        _info(f"No {label.lower()} rows in range.")
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
    chrome.searchable_table(
        out,
        key=f"{group}_pivot",
        search_col="k",
        money_cols=money_cols,
        rename={"k": label},
        pagination=15,
    )


def _cost_subcategory(group: str, end: date, sm: date) -> None:
    """Below-SKU cost breakdown, only where a connector stamps ``x_cost_subcategory``
    (currently: Redshift compute/concurrency-scaling/storage/spectrum-scan/serverless).
    Renders nothing for services that don't populate it.
    """
    df = gold_df(
        "SELECT service_name, cost_subcategory, sum(net_cost) AS net_cost "
        f'FROM "{group}".spend_by_cost_subcategory_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY service_name, cost_subcategory"
    )
    if df.empty:
        return
    chrome.panel_title("Cost subcategory breakdown")
    chrome.section_caption(
        "Spend below SKU granularity, where a connector supplies it "
        "(e.g. Redshift compute vs concurrency-scaling vs storage vs Spectrum scan)."
    )
    with ui.row().classes("w-full gap-4 flex-wrap"):
        for service_name, sub in df.groupby("service_name"):
            with ui.column().classes("gap-0").style("min-width:280px;flex:1;"):
                ui.label(str(service_name)).classes("text-sm").style(f"color:{chrome.INK_SECONDARY}")
                pie = px.pie(sub, names="cost_subcategory", values="net_cost", hole=0.45)
                pie.update_traces(textposition="inside", textinfo="percent+label")
                chrome.plot(chrome.style_fig(pie, has_legend=False, currency_axis=None))


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
    chrome.panel_title("Bill: list vs effective")
    chrome.section_caption(
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
            "Effective (paid)": chrome.SEMANTIC["paid"],
            "Savings (discount)": chrome.SEMANTIC["savings"],
        },
        category_orders={"component": ["Effective (paid)", "Savings (discount)"]},
        labels={"month": "", "cost": "", "component": ""},
    )
    chrome.plot(chrome.style_fig(fig, has_legend=True, category_x=True))


def _commitment(group: str, end: date, sm: date) -> None:
    """RI/Savings-Plan commitment coverage — Used vs Unused $. Renders nothing where
    the provider has no commitment data (e.g. Databricks — no system table exposes
    reservation/savings-plan data, see gold.commitment_summary_month's own docstring).
    """
    cm = gold_df(
        "SELECT commitment_discount_status, sum(effective_cost) AS cost "
        f'FROM "{group}".commitment_summary_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        "AND commitment_discount_status IS NOT NULL "
        "GROUP BY commitment_discount_status"
    )
    if cm.empty:
        return
    chrome.panel_title("Commitment coverage")
    unused = float(cm.loc[cm["commitment_discount_status"] == "Unused", "cost"].sum())
    total = float(cm["cost"].sum())
    pct = f"{100 * unused / total:.1f}%" if total else "—"
    chrome.section_caption(
        f"Reserved Instance / Savings Plan spend, split by whether it was drawn down "
        f"this window. {pct} of commitment spend was Unused — that's recoverable."
    )
    fig = px.bar(
        cm,
        x="commitment_discount_status",
        y="cost",
        color="commitment_discount_status",
        color_discrete_map={
            "Used": chrome.ACCENT,
            "Unused": chrome.WASTE,
        },
        labels={"commitment_discount_status": "", "cost": ""},
    )
    fig.update_layout(showlegend=False)
    chrome.plot(chrome.style_fig(fig, has_legend=False, category_x=True))


def _driver_mom(group: str, end: date) -> None:
    id_col, id_label, _ = driver_dim(group)
    chrome.panel_title(f"{id_label} month-over-month")
    # Compare the latest COMPLETE month (exclude the current, still-accruing month) so
    # we never pit a partial month against a full one.
    current = pd.Timestamp(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    months = gold_df(
        f'SELECT DISTINCT charge_month FROM "{group}".sku_month_over_month '
        f"WHERE charge_month <= '{end}' AND charge_month < '{current.date()}' "
        "ORDER BY charge_month DESC LIMIT 1"
    )
    if months.empty:
        chrome.section_caption("Not enough complete months in range to compare.")
        return
    cmp_month = pd.Timestamp(months.iloc[0]["charge_month"])
    prior = cmp_month - pd.DateOffset(months=1)
    chrome.section_caption(
        f"Top {id_label.lower()}s by net cost · {cmp_month:%b %Y} vs {prior:%b %Y}"
    )
    if group == "databricks":
        mom = gold_df(
            f"SELECT {id_col}, net_cost, cost_delta, cost_pct_change "
            f'FROM "{group}".sku_month_over_month WHERE charge_month = \'{cmp_month.date()}\' '
            "ORDER BY net_cost DESC LIMIT 20"
        )
        if mom.empty:
            _info(f"No {id_label.lower()} movement rows for the latest complete month.")
            return
        chrome.heatmap_table(
            mom,
            heat_col="cost_pct_change",
            key=f"{group}_mom",
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
        _info(f"No {id_label.lower()} movement rows for the latest complete month.")
        return
    chrome.searchable_table(
        svc.rename(columns={"k": id_col}),
        key=f"{group}_mom",
        search_col=id_col,
        money_cols=["net_cost", "cost_delta"],
        pct_cols=["cost_pct_change"],
        rename={
            id_col: id_label,
            "net_cost": "Net cost",
            "cost_delta": "Δ vs prior",
            "cost_pct_change": "MoM %",
        },
    )


def _tag_coverage(group: str, end: date, sm: date) -> None:
    """How much of the range's spend is attributable at all.

    The breakdown below drops untagged rows by construction, so without this a
    fully-untagged bill renders as a tidy, complete-looking tag table.

    Skipped (with a rebuild hint) on a lake published before this view existed — see
    :func:`gold_view_published`. The caption is the honest thing to show: the
    breakdown below is still correct, it just can't say what it omits.
    """
    if not gold_view_published(group, "spend_tag_coverage_month"):
        chrome.section_caption(
            "Tag coverage is unavailable until GOLD is rebuilt — run `flashlight transform`."
        )
        return
    cov = gold_df(
        "SELECT sum(gross_cost) AS gross, sum(tagged_cost) AS tagged, "
        f'sum(untagged_cost) AS untagged FROM "{group}".spend_tag_coverage_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    if cov.empty or not cov["gross"].iloc[0]:
        return
    gross = float(cov["gross"].iloc[0])
    tagged = float(cov["tagged"].iloc[0] or 0)
    untagged = float(cov["untagged"].iloc[0] or 0)
    chrome.section_caption(
        f"{tagged / gross:.0%} of charges in range carry a cost-allocation tag — "
        f"{compact_money(untagged)} is unattributed and absent from the breakdown below."
    )


def _tags(group: str, label: str, end: date, sm: date) -> None:
    keys = gold_df(
        f'SELECT tag_key, sum(net_cost) AS net FROM "{group}".spend_by_tag_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY tag_key ORDER BY net DESC"
    )
    chrome.panel_title("Spend by tag")
    _tag_coverage(group, end, sm)
    if keys.empty:
        _info("No tagged spend in range.")
        return

    options = keys["tag_key"].tolist()
    default = "team" if "team" in options else options[0]

    body_container = ui.column().classes("w-full gap-4")

    @ui.refreshable
    def _tag_values(sel: str) -> None:
        body_container.clear()
        with body_container:
            chrome.section_caption(f"{label} spend broken down by the `{sel}` tag")
            tags = gold_df(
                f"SELECT tag_value, sum(net_cost) AS net_cost FROM \"{group}\".spend_by_tag_month "
                f"WHERE tag_key = '{sel}' AND charge_month >= '{sm}' AND charge_month <= '{end}' "
                "GROUP BY tag_value ORDER BY net_cost DESC LIMIT 20"
            )
            if tags.empty:
                _info("No values for this tag in range.")
                return
            chrome.searchable_table(
                tags,
                key=f"{group}_tags",
                search_col="tag_value",
                money_cols=["net_cost"],
                rename={"tag_value": sel, "net_cost": "Net cost"},
            )

    (
        ui.select(options=options, value=default, on_change=lambda e: _tag_values.refresh(e.value))
        .props("dense outlined")
        .classes("w-48")
        .style(f"color:{chrome.INK_PRIMARY}")
    )
    _tag_values(default)
