"""Home — cross-provider spend headline and month-over-month movement."""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.chrome import DateState
from flashlight.dashboard.data import (
    gold_df,
    gold_last_updated,
    provider_label,
)
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.summary import cross_provider_movers
from flashlight.dashboard.theme import (
    compact_money,
    delta_variant,
    provider_color,
    provider_color_map,
)
from flashlight.transform.catalog import discover_provider_groups


def _headline_month(start: date, end: date) -> date | None:
    """Latest complete charge month within the range (excludes current partial month)."""
    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    sm = start.replace(day=1)
    cap = min(end.replace(day=1), (pd.Timestamp(current) - pd.DateOffset(months=1)).date())
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


def _recoverable_by_provider(month: date) -> pd.Series:
    """provider_name -> sum(recoverable_cost) for *month*; empty on any issue
    (efficiency.waste_record may not exist yet — no connector configured).
    """
    try:
        df = gold_df(
            "SELECT provider_name, sum(recoverable_cost) AS recoverable "
            f"FROM efficiency.waste_record WHERE charge_month = '{month}' "
            "GROUP BY provider_name"
        )
    except Exception:  # noqa: BLE001 - view may be unbuilt
        return pd.Series(dtype=float)
    return df.set_index("provider_name")["recoverable"]


def render() -> None:
    groups = discover_provider_groups()
    bounds_df = None
    for group in groups:
        b = gold_df(
            f'SELECT min(charge_day) AS lo, max(charge_day) AS hi FROM "{group}".spend_trend_daily'
        )
        if not b.empty and pd.notna(b["lo"].iloc[0]):
            bounds_df = b if bounds_df is None else bounds_df
    if not groups or bounds_df is None:
        chrome.section_title("Cloud spend overview")
        ui.label("No billing data yet.").classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    lo, hi = _d(bounds_df["lo"].iloc[0]), _d(bounds_df["hi"].iloc[0])
    date_state: DateState = {
        "start": max(lo, chrome.months_back(hi, 6)),
        "end": hi,
        "bounds_min": lo,
        "bounds_max": hi,
    }

    with ui.row().classes("items-center justify-between w-full"):
        chrome.section_title("Cloud spend overview")
        chrome.date_range_control(date_state, lambda: body.refresh())

    updated = gold_last_updated()
    cap = "Total spend across your connected cloud providers."
    if updated:
        cap += f" Data updated · {updated:%Y-%m-%d %H:%M} UTC."
    chrome.section_caption(cap)

    @ui.refreshable
    def body() -> None:
        start, end = date_state["start"], date_state["end"]
        month = _headline_month(start, end)
        if month is None:
            ui.label(
                "No complete billing month in the selected range — widen the date range "
                "or wait for the current month to finish."
            ).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
            return

        chrome.section_caption(
            f"Headline KPIs and charts use the latest complete month in your range: "
            f"{month:%b %Y} (range: {start:%b %d, %Y} → {end:%b %d, %Y})."
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
            ui.label("No provider spend rows for the latest complete month.").classes(
                "text-sm"
            ).style(f"color:{chrome.INK_MUTED}")
            return

        breakdown = pd.DataFrame(rows).sort_values("net_cost", ascending=False)
        total_delta = total_cur - total_prev
        total_pct = f"{100 * total_delta / total_prev:+.1f}%" if total_prev else "—"
        sign = "↑" if total_delta >= 0 else "↓"

        recoverable = _recoverable_by_provider(month)
        total_recoverable = float(recoverable.sum()) if not recoverable.empty else 0.0
        chrome.kpi_row(
            [
                (f"Total · {month:%b %Y}", compact_money(total_cur), "net across providers"),
                (
                    "Change vs prior month",
                    f"{'+' if total_delta >= 0 else '−'}{compact_money(abs(total_delta))}",
                    f"{sign} {total_pct} vs {prior:%b %Y}",
                    delta_variant(total_delta),
                ),
                (
                    "Recoverable this month",
                    compact_money(total_recoverable) if total_recoverable else "—",
                    f"{100 * total_recoverable / total_cur:.1f}% of spend"
                    if total_cur and total_recoverable
                    else "waste + opportunity",
                    "unattributed",
                ),
            ],
        )

        history = _provider_history(groups, start, end)
        with ui.row().classes("w-full gap-4 items-stretch"):
            with ui.column().classes("gap-0").style("flex:2;min-width:0;"):
                if not history.empty:
                    with chrome.panel():
                        chrome.panel_title("Spend trend by provider")
                        chrome.section_caption(
                            "Stacked net cost per month — each color is a cloud provider."
                        )
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
                        chrome.plot(chrome.style_fig(fig, has_legend=True, category_x=True))
            with ui.column().classes("gap-0").style("flex:1;min-width:0;"):
                with chrome.panel():
                    chrome.panel_title("Provider share")
                    chrome.section_caption(f"{month:%b %Y} net cost mix")
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
                    chrome.plot(chrome.style_fig(pie, has_legend=False, currency_axis=None))

        with chrome.panel():
            chrome.panel_title("Biggest movers")
            movers = cross_provider_movers(month, prior)
            if movers.empty:
                ui.label("No month-over-month movers in this range.").classes(
                    "text-sm"
                ).style(f"color:{chrome.INK_MUTED}")
            else:
                # Ranked table, not a bridge/waterfall chart: a mover's $ delta is
                # often a small fraction of the total spend it's measured against —
                # sharing one axis with the total crushes the small ones to invisible.
                # A table (plus the MoM % column) reads correctly regardless of scale,
                # matching how AWS Cost Explorer/Cloudability/CloudHealth show this.
                chrome.section_caption(
                    f"Largest absolute changes · {month:%b %Y} vs {prior:%b %Y}"
                )
                chrome.flat_table(
                    movers,
                    key="home_movers",
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

        with chrome.panel():
            chrome.panel_title("Provider details")
            with ui.row().classes("w-full gap-4 flex-wrap"):
                for row in breakdown.itertuples():
                    delta = float(row.delta)
                    pct = float(row.pct) if pd.notna(row.pct) else None
                    arrow = "↑" if delta >= 0 else "↓"
                    color = provider_color(label=str(row.provider), group=str(row.group))
                    delta_hex = (
                        color
                        if delta == 0
                        else chrome.SEMANTIC["increase" if delta > 0 else "decrease"]
                    )
                    # % alongside $ — a % reads correctly regardless of how large this
                    # provider's spend is, unlike comparing raw $ deltas across cards
                    # of very different size.
                    pct_text = f" ({pct:+.1f}%)" if pct is not None else ""
                    delta_text = (
                        f"{arrow} {compact_money(abs(delta))}{pct_text} vs {prior:%b %Y}"
                        if delta
                        else f"Flat vs {prior:%b %Y}"
                    )
                    rec = float(recoverable.get(row.provider, 0.0))
                    with ui.column().style("min-width:220px;flex:1;"):
                        chrome.provider_card(
                            name=f"{row.provider} · {month:%b %Y}",
                            amount=compact_money(float(row.net_cost)),
                            delta_text=delta_text,
                            color=color,
                            delta_color=delta_hex,
                            href=f"/{row.group}",
                            note=f"{compact_money(rec)} recoverable" if rec else None,
                        )

    body()
