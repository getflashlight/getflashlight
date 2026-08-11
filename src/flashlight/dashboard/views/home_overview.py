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
    gold_view_published,
    provider_label,
    provider_name_for_group,
)
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.summary import action_group_rows, cross_provider_movers
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


# Every figure on this page is `gross_cost` — sum(cost) over non-credit rows — not
# `net_cost`. A one-off credit (a negotiated goodwill credit, a refund) lands in a single
# month and nets against it, so at this altitude net reads as "spend collapsed" when
# nothing about the usage changed: an AWS Redshift goodwill credit made Jul 2026 look
# like a −$46K drop. Credits are real money and are NOT dropped — the note under the KPI
# row labels this page charges-only, and each credit line is itemized on the provider's own
# page (gold.credits_month).
# The provider pages still show net, which is the correct number for "what did I owe?".
_COST = "gross_cost"

# Home-page presentation: ``aws.*`` GOLD excludes Amazon S3 and Amazon EC2 (see
# ``silver.focus_provider_bill``). Databricks-backing buckets/instances live only in
# ``storage.backing_storage_month`` / ``compute.backing_compute_month`` as
# ``Databricks Storage`` / ``Databricks Compute``. This page folds that mapped spend
# into the Databricks stack so data-cloud spend isn't missing it.
# ``databricks.monthly_bill`` / Net Spend stay DBU-only — see
# ``views/provider_focus.py``'s ``footprint_card`` for the analogous, explicitly-labelled
# combined figure on the Databricks page itself.
_DBX_STORAGE_DRIVER = "Databricks Storage"
_STORAGE_GROUP = "storage"
_STORAGE_VIEW = "backing_storage_month"
_DBX_COMPUTE_DRIVER = "Databricks Compute"
_COMPUTE_GROUP = "compute"
_COMPUTE_VIEW = "backing_compute_month"


def _mapped_databricks_by_month(group: str, view: str, sm: date, cap: date) -> dict[date, float]:
    """``charge_month → gross_cost`` for ``mapping='databricks'`` rows in a backing-* view.

    Empty when the view is unpublished or has no managed rows — callers treat that as a
    no-op so a lake without the map keeps the raw provider split. Shared by
    ``_databricks_storage_by_month``/``_databricks_compute_by_month``: same shape
    (``mapping``/``gross_cost``/``charge_month``), different GOLD group.
    """
    if not gold_view_published(group, view):
        return {}
    try:
        df = gold_df(
            f"SELECT charge_month, sum(gross_cost) AS c FROM {group}.{view} "
            f"WHERE mapping = 'databricks' "
            f"AND charge_month >= '{sm}' AND charge_month <= '{cap}' "
            "GROUP BY charge_month"
        )
    except Exception:  # noqa: BLE001 - missing/stale view must not take the page down
        return {}
    if df.empty:
        return {}
    out: dict[date, float] = {}
    for row in df.itertuples(index=False):
        amount = float(row.c)
        if amount:
            out[_d(row.charge_month)] = amount
    return out


def _databricks_storage_by_month(sm: date, cap: date) -> dict[date, float]:
    return _mapped_databricks_by_month(_STORAGE_GROUP, _STORAGE_VIEW, sm, cap)


def _databricks_compute_by_month(sm: date, cap: date) -> dict[date, float]:
    return _mapped_databricks_by_month(_COMPUTE_GROUP, _COMPUTE_VIEW, sm, cap)


def _sum_by_month(*monthly: dict[date, float]) -> dict[date, float]:
    """Sum several ``charge_month → amount`` dicts (e.g. storage + compute) into one."""
    out: dict[date, float] = {}
    for d in monthly:
        for cm, amount in d.items():
            out[cm] = out.get(cm, 0.0) + amount
    return out


def _include_storage(
    group: str, cur: float, prev: float, storage_cur: float, storage_prev: float
) -> tuple[float, float]:
    """Add mapped backing spend (storage, compute, or both combined by the caller) onto
    the Databricks totals (aws.* already excludes the AWS-billed services behind it)."""
    if group == "databricks":
        return cur + storage_cur, prev + storage_prev
    return cur, prev


def _with_extra_databricks(history: pd.DataFrame, extra: dict[date, float]) -> pd.DataFrame:
    """Fold extra mapped spend (storage or compute) into the Databricks stack of the
    spend-trend chart. Called once per backing-* plane — each call only ever touches
    the ``databricks`` rows, so calling it twice (storage, then compute) composes
    correctly without a combined-dict merge step."""
    if history.empty or not extra:
        return history
    hist = history.copy()
    hist["_cm"] = pd.to_datetime(hist["charge_month"]).dt.date
    extras: list[dict[str, object]] = []
    dbx_label = provider_label("databricks")
    for cm, amount in extra.items():
        dbx_mask = (hist["group"] == "databricks") & (hist["_cm"] == cm)
        if dbx_mask.any():
            hist.loc[dbx_mask, "net_cost"] = hist.loc[dbx_mask, "net_cost"] + amount
        else:
            extras.append(
                {
                    "charge_month": cm,
                    "net_cost": amount,
                    "provider": dbx_label,
                    "group": "databricks",
                    "month": pd.Timestamp(cm).strftime("%Y-%m"),
                    "_cm": cm,
                }
            )
    if extras:
        hist = pd.concat([hist, pd.DataFrame(extras)], ignore_index=True)
    hist = hist[hist["net_cost"].abs() > 1e-9]
    return hist.drop(columns=["_cm"]).sort_values("charge_month")


def _home_movers(month: date, prior: date, *, limit: int = 8) -> pd.DataFrame:
    """Biggest movers plus Databricks Storage/Compute MoM from their backing-* GOLD planes.

    ``aws.*`` no longer carries Amazon S3 or Amazon EC2, so there is nothing to rename or
    residual-split — only inject each mapped delta under Databricks, as its own driver row
    (never combined into one "Databricks backing spend" row — a reader asking "what moved"
    needs to know whether it was the storage bill or the compute bill that moved).

    When ACCOUNT_USAGE data is available, GOLD ``snowflake.*`` service movers are replaced
    by Snowflake compute (Managed + Serverless) MoM so the table matches Visibility.
    """
    movers = cross_provider_movers(month, prior, exclude_credits=True, limit=max(limit, 32))
    frames: list[pd.DataFrame] = []
    sf_label = provider_label("snowflake")
    sf_compute = _snowflake_compute_by_month()
    if not movers.empty:
        if sf_compute:
            movers = movers[movers["provider"].astype(str) != sf_label]
        if not movers.empty:
            frames.append(movers)
    if "databricks" in discover_provider_groups():
        for by_month, driver, kind in (
            (_databricks_storage_by_month(prior, month), _DBX_STORAGE_DRIVER, "Storage"),
            (_databricks_compute_by_month(prior, month), _DBX_COMPUTE_DRIVER, "Compute"),
        ):
            mapped_cur = by_month.get(month, 0.0)
            mapped_prev = by_month.get(prior, 0.0)
            mapped_delta = mapped_cur - mapped_prev
            if abs(mapped_delta) > 1e-9:
                frames.append(
                    pd.DataFrame(
                        [
                            {
                                "provider": provider_label("databricks"),
                                "driver": driver,
                                "cost_delta": mapped_delta,
                                "cost_pct_change": (
                                    round(100 * mapped_delta / mapped_prev, 1)
                                    if mapped_prev
                                    else None
                                ),
                                "type": kind,
                            }
                        ]
                    )
                )
    sf_frame: pd.DataFrame | None = None
    if sf_compute:
        cur = float(sf_compute.get(month, 0.0))
        prev = float(sf_compute.get(prior, 0.0))
        delta = cur - prev
        if abs(delta) > 1e-9:
            sf_frame = pd.DataFrame(
                [
                    {
                        "provider": sf_label,
                        "driver": "Compute",
                        "cost_delta": delta,
                        "cost_pct_change": round(100 * delta / prev, 1) if prev else None,
                        "type": "Compute",
                    }
                ]
            )
            frames.append(sf_frame)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["abs_delta"] = out["cost_delta"].abs()
    out = out.sort_values("abs_delta", ascending=False).reset_index(drop=True)
    # Keep Snowflake compute visible even when its MoM $ is smaller than SKU movers.
    if sf_frame is not None and limit > 1:
        sf_mask = out["provider"].astype(str).eq(sf_label)
        others = out.loc[~sf_mask].head(limit - 1)
        snow = out.loc[sf_mask].head(1)
        out = pd.concat([others, snow], ignore_index=True)
        out["abs_delta"] = out["cost_delta"].abs()
        out = out.sort_values("abs_delta", ascending=False).reset_index(drop=True)
    else:
        out = out.head(limit)
    return out.drop(columns=["abs_delta"])


def _provider_months(group: str, month: date, prior: date) -> tuple[float, float]:
    row = gold_df(
        f"SELECT coalesce(sum({_COST}) FILTER (WHERE charge_month = '{month}'), 0) AS cur, "
        f"coalesce(sum({_COST}) FILTER (WHERE charge_month = '{prior}'), 0) AS prev "
        f'FROM "{group}".monthly_bill'
    ).iloc[0]
    return float(row["cur"]), float(row["prev"])


def _provider_display_order(groups: list[str]) -> list[str]:
    """Databricks → Snowflake → Redshift (aws), then any other groups alphabetically."""
    lead = ("databricks", "snowflake", "aws")
    present = set(groups)
    ordered = [g for g in lead if g in present]
    ordered.extend(sorted(g for g in present if g not in lead))
    return ordered


def _snowflake_data_module() -> object | None:
    """Live ACCOUNT_USAGE when configured, else bundled synthetic Parquet."""
    from flashlight.dashboard.snowflake import live_data, visibility_data

    if live_data.is_configured():
        return live_data
    if visibility_data.has_synthetic_data():
        return visibility_data
    return None


def _snowflake_tco_by_month() -> dict[date, float]:
    sf = _snowflake_data_module()
    if sf is None:
        return {}
    return dict(sf.tco_by_month())  # type: ignore[attr-defined]


def _snowflake_compute_by_month() -> dict[date, float]:
    """Managed + Serverless compute cost by month-start date from ACCOUNT_USAGE."""
    sf = _snowflake_data_module()
    if sf is None or not hasattr(sf, "cost_breakdown_monthly"):
        return {}
    df = sf.cost_breakdown_monthly(12)  # type: ignore[attr-defined]
    if df is None or getattr(df, "empty", True):
        return {}
    compute = df[df["category"].isin(["Managed Compute", "Serverless Compute"])]
    if compute.empty:
        return {}
    by_month = compute.groupby("month", as_index=True)["cost_usd"].sum()
    out: dict[date, float] = {}
    for key, amount in by_month.items():
        month = pd.Timestamp(str(key) + "-01" if len(str(key)) == 7 else str(key)).date()
        month = month.replace(day=1)
        out[month] = float(amount)
    return out


def _with_snowflake_tco(
    history: pd.DataFrame, tco: dict[date, float], start: date, end: date
) -> pd.DataFrame:
    """Replace GOLD snowflake monthly_bill with ACCOUNT_USAGE TCO in the trend."""
    if not tco:
        return history
    frames: list[pd.DataFrame] = []
    if not history.empty:
        frames.append(history[history["group"] != "snowflake"])
    rows = [
        {
            # Match GOLD history's datetime64 charge_month — mixing date with
            # Timestamp breaks sort_values under pandas 2.x.
            "charge_month": pd.Timestamp(month),
            "net_cost": amount,
            "provider": provider_label("snowflake"),
            "group": "snowflake",
            "month": month.strftime("%Y-%m"),
        }
        for month, amount in sorted(tco.items())
        if start <= month <= end
    ]
    if rows:
        frames.append(pd.DataFrame(rows))
    if not frames:
        return history
    return pd.concat(frames, ignore_index=True).sort_values("charge_month")


def _credits_by_group(month: date) -> dict[str, float]:
    """group → credits/adjustments applied in *month* (negative), for groups with any.

    Skips a group whose lake predates ``gold.credits_month`` (published but never
    re-transformed) rather than taking the page down — same guard as every other
    newer-view read.
    """
    out: dict[str, float] = {}
    for group in discover_provider_groups():
        if not gold_view_published(group, "credits_month"):
            continue
        df = gold_df(
            f'SELECT coalesce(sum(net_cost), 0) AS credits FROM "{group}".credits_month '
            f"WHERE charge_month = '{month}'"
        )
        credits = float(df["credits"].iloc[0]) if not df.empty else 0.0
        if credits:
            out[group] = credits
    return out


def _provider_history(groups: list[str], start: date, end: date) -> pd.DataFrame:
    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    sm = start.replace(day=1)
    cap = min(end.replace(day=1), (pd.Timestamp(current) - pd.DateOffset(months=1)).date())
    frames: list[pd.DataFrame] = []
    for group in groups:
        label = provider_label(group)
        df = gold_df(
            f'SELECT charge_month, {_COST} AS net_cost FROM "{group}".monthly_bill '
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
    history = pd.concat(frames, ignore_index=True).sort_values("charge_month")
    if "databricks" not in groups:
        return history
    history = _with_extra_databricks(history, _databricks_storage_by_month(sm, cap))
    return _with_extra_databricks(history, _databricks_compute_by_month(sm, cap))


def _recoverable_by_provider(month: date) -> pd.Series:
    """Provider -> conservative actionable-savings potential for *month*.

    This deliberately shares the Efficiency & Waste action queue's roll-up: the best
    priced finding per entity/lens, rather than summing overlapping findings. The two
    remedy lanes remain distinct in that tab, but this home-page total is their combined
    navigation summary and carries a non-additivity note wherever it is rendered.

    Snowflake uses Visibility hidden-waste savings when that plane is available
    (same total as the Snowflake page), not FOCUS ``efficiency.waste_record``.
    """
    values: dict[str, float] = {}
    try:
        df = gold_df(
            "SELECT * FROM efficiency.waste_record "
            f"WHERE charge_month = '{month}'"
        )
    except Exception:  # noqa: BLE001 - view may be unbuilt
        df = pd.DataFrame()
    if not df.empty:
        # AWS can carry non-Redshift records (for example S3 storage), while the AWS
        # provider page is explicitly the Redshift view. Keep this headline scoped to the
        # Efficiency & Waste surfaces a reader can reconcile it against.
        aws = df["provider_name"].astype(str).eq("AWS")
        redshift_aws = df["waste_category"].astype(str).str.startswith("redshift_") | df[
            "entity_type"
        ].isin(["sql_warehouse", "sql_warehouse_user"])
        df = df.loc[~aws | redshift_aws]
        values = {
            str(provider): float(action_group_rows(rows)["potential_savings"].sum())
            for provider, rows in df.groupby("provider_name")
        }
    sf = _snowflake_data_module()
    if sf is not None:
        values.pop(provider_name_for_group("snowflake"), None)
        values.pop("Snowflake", None)
        waste = float(sf.hidden_waste_summary().get("total") or 0)  # type: ignore[attr-defined]
        if waste:
            values[provider_name_for_group("snowflake")] = waste
    return pd.Series(values, dtype=float)


def _savings_providers_caption(recoverable: pd.Series) -> str:
    """Short list of providers contributing to the actionable-savings KPI."""
    labels: list[str] = []
    name_to_label = {
        "Databricks": "Databricks",
        "Snowflake": "Snowflake",
        "AWS": "Redshift",
    }
    for name, label in name_to_label.items():
        if name in recoverable.index and float(recoverable.get(name, 0.0) or 0.0) > 0:
            labels.append(label)
    if not labels:
        return "tune + move options"
    return " + ".join(labels)


def _credits_note(month: date) -> None:
    """Label the KPIs above for what they leave out — rendered only when there are
    credits, so a bill with no credits carries no extra chrome.

    Deliberately generic: no totals, month or provider names. Those belong to the
    line items on each provider's own page (``gold.credits_month``), and restating
    them here made the label longer than the figures it qualifies.
    """
    if not _credits_by_group(month):
        return
    chrome.section_caption("Charges only — credits excluded.")


def render() -> None:
    groups = discover_provider_groups()
    # The union across every provider's own span, not whichever group happens to be
    # first alphabetically (discover_provider_groups() sorts by name) — this page is
    # the cross-provider one, so its date bounds (and therefore its YTD default, which
    # anchors off `hi`) must reflect the latest/earliest date ANY provider has, not just
    # the first one iterated. Picking one group's bounds silently narrowed the range
    # (or anchored YTD on a stale year) whenever a later-sorted provider had newer data.
    lo: date | None = None
    hi: date | None = None
    for group in groups:
        b = gold_df(
            f'SELECT min(charge_day) AS lo, max(charge_day) AS hi FROM "{group}".spend_trend_daily'
        )
        if b.empty or pd.isna(b["lo"].iloc[0]):
            continue
        g_lo, g_hi = _d(b["lo"].iloc[0]), _d(b["hi"].iloc[0])
        lo = g_lo if lo is None else min(lo, g_lo)
        hi = g_hi if hi is None else max(hi, g_hi)
    if not groups or lo is None or hi is None:
        chrome.section_title("Data Cloud Spend overview")
        ui.label("No billing data yet.").classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return
    date_state: DateState = {
        # YTD, matching every provider page's default (see provider_focus.render for why) —
        # the two surfaces are compared constantly, so they must open on the same window.
        "start": max(lo, chrome.year_start(hi)),
        "end": hi,
        "bounds_min": lo,
        "bounds_max": hi,
    }

    with ui.row().classes("items-center justify-between w-full"):
        chrome.section_title("Data Cloud Spend overview")
        chrome.date_range_control(date_state, lambda: body.refresh())

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

        prior = (pd.Timestamp(month) - pd.DateOffset(months=1)).date()
        # Fold Databricks Storage + Databricks Compute into Databricks totals (aws.*
        # already excludes S3/EC2 — see silver.focus_provider_bill). Combined into one
        # amount here (unlike _home_movers, which keeps them as separate driver rows) —
        # the KPI row is a single "Databricks" total, not a per-source breakdown.
        backing = _sum_by_month(
            _databricks_storage_by_month(prior, month),
            _databricks_compute_by_month(prior, month),
        ) if "databricks" in groups else {}
        storage_cur = backing.get(month, 0.0)
        storage_prev = backing.get(prior, 0.0)
        # Snowflake Visibility TCO (complete months) — replaces thin FOCUS
        # snowflake.monthly_bill when live/synthetic ACCOUNT_USAGE is available.
        sf_tco = _snowflake_tco_by_month()
        # Build provider rows in nav order (Databricks → Snowflake → Redshift).
        display_groups = _provider_display_order(
            list(groups) + (["snowflake"] if sf_tco and "snowflake" not in groups else [])
        )
        rows: list[dict[str, object]] = []
        total_cur = total_prev = 0.0
        for group in display_groups:
            label = provider_label(group)
            if group == "snowflake" and sf_tco:
                cur = float(sf_tco.get(month, 0.0))
                prev = float(sf_tco.get(prior, 0.0))
            elif group == "snowflake" and group not in groups:
                continue
            else:
                cur, prev = _provider_months(group, month, prior)
                cur, prev = _include_storage(group, cur, prev, storage_cur, storage_prev)
            if not cur and not prev:
                continue
            delta = cur - prev
            pct = 100 * delta / prev if prev else None
            rows.append(
                {
                    "group": group,
                    "provider": label,
                    # The raw provider_name too: `provider` is a display label and no
                    # longer always equals it (see data._GROUP_LABEL_OVERRIDES), so it
                    # can't be used to look a provider up in another view's rows.
                    "provider_name": provider_name_for_group(group),
                    "net_cost": cur,
                    "delta": delta,
                    "pct": pct,
                }
            )
            total_cur += cur
            total_prev += prev

        if not rows:
            ui.label("No provider spend rows for the latest complete month.").classes(
                "text-sm"
            ).style(f"color:{chrome.INK_MUTED}")
            return

        # Preserve Databricks → Snowflake → Redshift card order (not spend rank).
        breakdown = pd.DataFrame(rows)
        total_delta = total_cur - total_prev
        total_pct = f"{100 * total_delta / total_prev:+.1f}%" if total_prev else "—"
        sign = "↑" if total_delta >= 0 else "↓"

        recoverable = _recoverable_by_provider(month)
        total_recoverable = float(recoverable.sum()) if not recoverable.empty else 0.0
        savings_who = _savings_providers_caption(recoverable)
        chrome.kpi_row(
            [
                (
                    f"TCO - {month:%b %Y} MTD",
                    compact_money(total_cur),
                    "across providers",
                ),
                (
                    "Change vs prior month",
                    f"{'+' if total_delta >= 0 else '−'}{compact_money(abs(total_delta))}",
                    f"{sign} {total_pct} vs {prior:%b %Y}",
                    delta_variant(total_delta),
                ),
                (
                    "Actionable savings potential",
                    compact_money(total_recoverable) if total_recoverable else "—",
                    f"{100 * total_recoverable / total_cur:.1f}% of spend · {savings_who}"
                    if total_cur and total_recoverable
                    else savings_who,
                    "unattributed",
                ),
            ],
        )

        _credits_note(month)

        history = _provider_history(groups, start, end)
        sm, cap = start.replace(day=1), end.replace(day=1)
        history = _with_snowflake_tco(history, sf_tco, sm, cap)
        trend_extras: list[str] = []
        if "databricks" in groups:
            if _databricks_storage_by_month(sm, cap):
                trend_extras.append("storage (AWS-billed S3)")
            if _databricks_compute_by_month(sm, cap):
                trend_extras.append("compute (AWS-billed EC2)")
        trend_caption = (
            "Stacked monthly charges — Databricks includes its managed "
            + " and ".join(trend_extras) + "."
            if trend_extras
            else "Stacked monthly charges — each color is a cloud provider."
        )
        with ui.row().classes("w-full gap-4 items-stretch"):
            with ui.column().classes("gap-0").style("flex:2;min-width:0;"):
                if not history.empty:
                    with chrome.panel():
                        chrome.panel_title("Spend trend by provider")
                        chrome.section_caption(trend_caption)
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
                        month_totals = history.groupby("month")["net_cost"].sum()
                        for bar_month, total in month_totals.items():
                            fig.add_annotation(
                                x=bar_month,
                                y=total,
                                text=compact_money(float(total)),
                                showarrow=False,
                                yshift=10,
                                font=dict(size=11, color=chrome.INK_SECONDARY),
                            )
                        chrome.plot(chrome.style_fig(fig, has_legend=True, category_x=True))
            with ui.column().classes("gap-0").style("flex:1;min-width:0;"):
                with chrome.panel():
                    chrome.panel_title("Provider share")
                    chrome.section_caption(f"{month:%b %Y} MTD charge")
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
            movers = _home_movers(month, prior)
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
                    rec = float(recoverable.get(row.provider_name, 0.0))
                    note = f"{compact_money(rec)} action potential" if rec else None
                    with ui.column().style("min-width:220px;flex:1;"):
                        chrome.provider_card(
                            name=f"{row.provider} · {month:%b %Y}",
                            amount=compact_money(float(row.net_cost)),
                            delta_text=delta_text,
                            color=color,
                            delta_color=delta_hex,
                            href=f"/{row.group}",
                            note=note,
                        )

    body()
