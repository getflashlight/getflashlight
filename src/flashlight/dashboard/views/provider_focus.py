"""Per-provider FOCUS spend — one page per provider, rendered from its GOLD group.

Each provider's GOLD lives in its own group schema (``aws.*``, ``databricks.*``, …),
so the page reads ``<group>.<view>`` with no ``provider_name`` filter. A per-page
date range picker (own state, not shared across page navigations — see
``router.py``) drives every panel below it. ``render`` is bound per provider by
``router.build_pages()``.

Every provider page carries the same five core tabs — **Trend & changes**, **Breakdown**,
**Attribution** (``views/attribution.py``), **Efficiency & Waste**
(``views/efficiency_waste.py``), **Policy Compliance** (``views/policy.py``), and,
where enabled, **Alerts** (``views/alerts.py``, always last) — plus caller-supplied
extras.
``after_breakdown`` inserts spend-detail tabs (Databricks: AI Costs, Databricks Storage)
right after Breakdown; ``extra_tabs`` follow the core tabs (Databricks: Client Driver
Health). Attribution and Efficiency & Waste used to be cross-provider pages,
``/leaderboard`` and ``/utilization``; they are per-provider now because both answer
questions you ask *about a bill*. Policy Compliance used to be a Databricks-only extra
tab, which hid rows other providers were already producing — see ``views/policy.py``.
Alerts holds the MoM callout that used to sit as a header caption under the KPIs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
import plotly.express as px
from nicegui import ui
from nicegui.events import GenericEventArguments

from flashlight.dashboard import chrome, router
from flashlight.dashboard.chrome import DateState
from flashlight.dashboard.data import gold_df, gold_view_published, provider_name_for_group
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.summary import _service_movers, driver_dim
from flashlight.dashboard.theme import compact_money, delta_variant, provider_color, rgba_hex
from flashlight.dashboard.views import alerts, attribution, efficiency_waste, policy
from flashlight.transform.catalog import provider_view_dimensions


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _info(text: str) -> None:
    ui.label(text).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


@dataclass(frozen=True)
class Scope:
    """A page's data scope: one GOLD group, optionally narrowed to a subset of one
    dimension's values inside it.

    Every provider page is the whole group (``Scope("databricks")``). ``/aws`` is the
    only narrowed one: the ``aws`` group restricted to Redshift's own FOCUS service
    names, because the ``aws_focus`` connector ingests a Redshift-scoped slice of the
    bill. That narrowing is why this exists at all — without it the two pages were two
    implementations of the same four tabs.

    A view under a narrowed scope is one of **three** things, and collapsing it to two
    is what forced that fork:

    * **scopable** — carries the narrowing dimension, so the predicate applies.
    * **account-wide** — belongs to the whole account by nature. Declared in
      *account_wide*, checked BEFORE the "does it carry the dimension?" test, because
      ``credits_month`` *does* carry ``service_name`` and must still be read
      account-wide: AWS applies a credit at account level and often tags it to no
      service, so filtering would hide part of the discount.
    * **unavailable** — no narrowing dimension, and the number would be wrong under a
      narrowed heading (a group total, a percentage, a per-SKU variance). :meth:`available`
      is False and the panel must say why it's absent, never widen silently. A scope may
      explicitly opt into a local forecast fit from its own daily costs; that is not an
      unscoped read of the provider forecast.

    A panel that takes a plain ``group: str`` rather than a ``Scope`` is one that reads
    the whole group *by design* — see ``_credits``/``_commitment``.
    """

    group: str
    dimension: str | None = None
    values: tuple[str, ...] = ()
    account_wide: frozenset[str] = field(default_factory=frozenset)
    scoped_forecast: bool = False

    @property
    def narrowed(self) -> bool:
        return self.dimension is not None and bool(self.values)

    def carries(self, view: str) -> bool:
        """True when *view* declares this scope's narrowing dimension.

        Read off the GOLD catalog rather than a local list, so widening a view (adding
        ``service_name`` to ``spend_trend_daily``, say) makes it scopable here with no
        second edit.
        """
        return self.dimension in provider_view_dimensions(view)

    def available(self, view: str) -> bool:
        """False when *view* cannot honestly serve this scope — see the class docstring."""
        if not self.narrowed:
            return True
        return view in self.account_wide or self.carries(view)

    def predicate(self, view: str) -> str:
        """This scope's own SQL predicate for *view*; ``""`` when none applies."""
        if not self.narrowed or view in self.account_wide:
            return ""
        if not self.carries(view):
            raise ValueError(
                f"{self.group}.{view} carries no {self.dimension} — check "
                "Scope.available() first, or declare the view account-wide"
            )
        vals = ", ".join(f"'{_q(v)}'" for v in self.values)
        return f"{self.dimension} IN ({vals})"

    def where(self, view: str, *clauses: str) -> str:
        """A complete ``WHERE …`` clause (or ``""``): *clauses* ANDed with this scope's
        predicate for *view*, empty ones dropped.

        For an un-narrowed scope this is exactly ``WHERE <clauses>`` — byte-identical to
        what every panel built before this existed, which is what makes the conversion
        safe and is pinned by a test over every provider base view.
        """
        parts = [c for c in (*clauses, self.predicate(view)) if c]
        return f"WHERE {' AND '.join(parts)}" if parts else ""


def render(
    group: str,
    label: str,
    *,
    after_breakdown: Sequence[tuple[str, Callable[[date, date], None]]] = (),
    extra_tabs: Sequence[tuple[str, Callable[[], None]]] = (),
    scope: Scope | None = None,
    scope_caption: Callable[[date, date], None] | None = None,
    breakdown_lead: Sequence[Callable[[date, date], None]] = (),
    extra_kpis: Sequence[Callable[[date, date], chrome.KpiCard | None]] = (),
    attribution_tab: Callable[[date, date], None] | None = None,
    efficiency_tab: Callable[[date, date], None] | None = None,
    efficiency_tab_label: str = "Efficiency & Waste",
    show_policy: bool = True,
    show_alerts: bool = True,
    show_daily_trend: bool = True,
    show_monthly_forecast: bool = True,
    show_current_month_projection: bool = True,
    monthly_chart_label: str = "Monthly net cost",
    invoice_explanations_in_trend: bool = False,
    show_credit_kpi: bool = True,
    combine_sku_spend_and_mom: bool = False,
) -> None:
    """Render one provider's page.

    The keyword arguments are what let ``/aws`` be a *configuration* of this page rather
    than a second implementation of it (see ``views/redshift_focus.py``):

    * *scope* narrows every panel to a subset of one dimension's values — ``None`` means
      the whole group, which is every other provider.
    * *scope_caption* states the narrowing (and what it therefore hides) under the title.
    * *breakdown_lead* prepends provider-shaped panels to the Breakdown tab.
    * *after_breakdown* inserts provider-shaped tabs immediately after Breakdown (spend
      detail that belongs next to composition — Databricks' AI Costs and Storage). Each
      callable receives ``(sm, end)`` so those tabs share the page date range.
    * *extra_tabs* inserts tabs after the core tabs (ops/compliance signals that aren't
      spend composition — Databricks' Client Driver Health).
    * *extra_kpis* appends provider-shaped cards to the KPI row — each returning ``None``
      when it has nothing to report, so a page never carries a "$0" card. A card here is
      *beside* the headline, never a term in it: Databricks' backing storage is billed by
      AWS, so the card names its own biller and says it is not inside the net figure
      (see ``views/backing_storage.kpi_card``).
    * *attribution_tab* / *efficiency_tab* replace a whole tab, for a page whose version of it
      is structured differently — Redshift's findings are faceted per *cluster*, finer
      than per provider, so the shared tab beside them would render the same
      ``waste_record`` rows twice; and its owner/tag panels are account-wide and have to
      say so.
    * *show_alerts* controls the optional Alerts tab. It remains last when enabled;
      Databricks disables it.
    * *show_daily_trend* / *show_monthly_forecast* let a provider keep the shared
      monthly chart while omitting a daily plot or forecast treatment that is not useful.

    Provider-specific labels and optional tabs are deliberately explicit here, so their
    visual differences do not require a fork of the common cost-page implementation.
    """
    sc = scope if scope is not None else Scope(group)
    bounds = gold_df(
        f'SELECT min(charge_day) AS lo, max(charge_day) AS hi FROM "{group}".spend_trend_daily '
        + sc.where("spend_trend_daily")
    )
    if bounds.empty or pd.isna(bounds["lo"].iloc[0]):
        # A narrowed page has two different reasons to be empty and must not conflate
        # them: the group genuinely has no billing data, or it has plenty but none of it
        # is in this page's scope. "Enable the connection" is wrong (and alarming) advice
        # for the second.
        if sc.narrowed and not gold_df(f'SELECT 1 FROM "{group}".spend_trend_daily LIMIT 1').empty:
            _info(
                f"No {label} spend found in the {group.upper()} bill. The account is "
                f"connected and billing data is present, but none of it is "
                f"{label}'s — nothing here is scoped to it."
            )
        else:
            _info(f"No {label} billing data found. Your admin may need to enable the connection.")
        return

    lo, hi = _d(bounds["lo"].iloc[0]), _d(bounds["hi"].iloc[0])
    date_state: DateState = {
        # YTD, not a rolling 6 months: a finance question ("what have we spent this year?")
        # has a fixed anchor, and a rolling window silently redraws every month, so the same
        # page compared week to week isn't the same window. `chrome.year_start` reads off the
        # data's last month, so a stale lake opens on its own last year rather than on an
        # empty January. Early in a calendar year this window is genuinely short — that's the
        # honest answer to "year to date", and 6mo/12mo are one click away.
        "start": max(lo, chrome.year_start(hi)),
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
        if scope_caption is not None:
            scope_caption(sm, end)

        _kpis(
            sc,
            label,
            start,
            end,
            sm,
            partial=partial,
            extra_kpis=extra_kpis,
            show_credit_kpi=show_credit_kpi,
        )

        # Ordered tab bar: core spend → after_breakdown (Databricks spend detail) →
        # attribution/efficiency/policy → extra_tabs (ops signals) → optional Alerts.
        def _panel_trend() -> None:
            if show_daily_trend:
                with chrome.panel():
                    _trend(sc, label, start, end, accent=accent)
            with chrome.panel():
                # Forecast shares this chart's axis (not a panel of its own) so a
                # projection can't out-scale an actual month.
                _monthly_drill(
                    sc,
                    label,
                    end,
                    sm,
                    accent=accent,
                    show_forecast=show_monthly_forecast,
                    show_current_month_projection=show_current_month_projection,
                    chart_label=monthly_chart_label,
                )
            if invoice_explanations_in_trend:
                # Keep the two visual explanations together on desktop, like the home
                # dashboard's trend/share row; narrow screens naturally stack them.
                with ui.row().classes("w-full gap-4 items-stretch flex-wrap"):
                    _cost_subcategory(sc, end, sm, panel_class="flex-1 min-w-80")
                _credits(group, end, sm)

        def _panel_breakdown() -> None:
            # One panel per section — same as Trend & changes. Helpers that can
            # render nothing (subcategory / credits) wrap themselves
            # so an empty bill doesn't leave blank cards. Lead hooks (Redshift's
            # spend partition) own their own panels too.
            for lead in breakdown_lead:
                lead(sm, end)
            if combine_sku_spend_and_mom:
                # Redshift's two useful billing cuts answer complementary questions:
                # the donut shows *what* makes up the invoice, while the SKU table
                # shows *which line items changed* in the latest closed month. Keep
                # them in one card so that relationship is visible without making
                # readers stitch together separate dashboard sections.
                with chrome.panel():
                    chrome.panel_title("Redshift breakdown")
                    with ui.row().classes("w-full gap-6 items-start flex-wrap"):
                        with ui.column().classes("flex-1 min-w-80 gap-0"):
                            _cost_subcategory(sc, end, sm, embedded=True)
                        with ui.column().classes("min-w-96 gap-0").style("flex:2 1 32rem;"):
                            _driver_mom(sc, end, sku_mom_scoped=True)
            else:
                with chrome.panel():
                    _spend_pivot(sc, end, sm)
                if not invoice_explanations_in_trend:
                    _cost_subcategory(sc, end, sm)
                with chrome.panel():
                    _driver_mom(sc, end, sku_mom_scoped=False)
            _credits(group, end, sm)

        def _panel_attribution() -> None:
            # Own panels — no chrome.panel() wrapper (see views/attribution.py).
            if attribution_tab is not None:
                attribution_tab(sm, end)
            else:
                attribution.render(group, end, sm)

        def _panel_efficiency() -> None:
            if efficiency_tab is not None:
                efficiency_tab(sm, end)
            else:
                efficiency_waste.render(provider_name_for_group(group), label, sm, end)

        def _panel_policy() -> None:
            policy.render(provider_name_for_group(group), label, end, sm)

        def _panel_alerts() -> None:
            # monthly_bill has no service dimension, so a narrowed page reads its
            # headline from the service-dimensioned view instead — otherwise MoM
            # would report the whole account's spend under a narrowed heading.
            alert_view = (
                "monthly_bill" if sc.available("monthly_bill") else "spend_by_service_month"
            )
            alerts.render(
                group,
                label,
                start,
                end,
                partial=partial,
                bill_view=alert_view,
                scope_sql=sc.predicate(alert_view),
            )

        # after_breakdown callables take (sm, end); wrap so the tab bar stays
        # Callable[[], None] for every entry.
        after = [(title, (lambda fn=fn: fn(sm, end))) for title, fn in after_breakdown]

        tab_specs: list[tuple[str, Callable[[], None]]] = [
            ("Trend & changes", _panel_trend),
            ("Breakdown", _panel_breakdown),
            *after,
            ("Attribution", _panel_attribution),
            (efficiency_tab_label, _panel_efficiency),
            *([("Policy Compliance", _panel_policy)] if show_policy else []),
            *extra_tabs,
        ]
        if show_alerts:
            tab_specs.append(("Alerts", _panel_alerts))
        # Only the active tab's content is built (chrome.lazy_tab_panels) — every
        # other tab's queries/charts wait until the user actually clicks it, rather
        # than all ~9 tabs paying for themselves on every load (see that helper's
        # docstring for the measured cost this avoids on /databricks).
        chrome.lazy_tab_panels(tab_specs)

    body()


def _bill_months(scope: Scope, sm: date, end: date) -> pd.DataFrame:
    """``(charge_month, net_cost, list_cost, savings)`` for *scope*, month-ordered.

    ``monthly_bill`` for a whole group; the service-dimensioned
    ``spend_by_service_month`` aggregated to month when narrowed, since ``monthly_bill``
    carries no service dimension and would report the entire account under a narrowed
    heading. Both are the same ``sum(cost)``/``sum(list_cost)`` over the same
    ``silver.focus_normalized``, only grouped finer, so they agree by construction —
    that reconciliation is pinned in ``tests/test_lake_roundtrip.py`` and is the reason
    ``list_cost``/``savings`` were added to the service view rather than a service
    dimension being bolted onto the headline one.
    """
    months = f"charge_month >= '{sm}' AND charge_month <= '{end}'"
    if scope.available("monthly_bill"):
        return gold_df(
            "SELECT charge_month, net_cost, list_cost, savings "
            f'FROM "{scope.group}".monthly_bill {scope.where("monthly_bill", months)} '
            "ORDER BY charge_month"
        )
    return gold_df(
        "SELECT charge_month, sum(net_cost) AS net_cost, sum(list_cost) AS list_cost, "
        f'sum(savings) AS savings FROM "{scope.group}".spend_by_service_month '
        f"{scope.where('spend_by_service_month', months)} "
        "GROUP BY charge_month ORDER BY charge_month"
    )


def _kpis(
    scope: Scope,
    label: str,
    start: date,
    end: date,
    sm: date,
    *,
    partial: bool,
    extra_kpis: Sequence[Callable[[date, date], chrome.KpiCard | None]] = (),
    show_credit_kpi: bool = True,
) -> None:
    bills = _bill_months(scope, sm, end)
    if bills.empty:
        _info(f"No {label} spend in the selected range.")
        return
    net = float(bills["net_cost"].sum())
    lst = float(bills["list_cost"].sum())
    sav = float(bills["savings"].sum())
    disc = f"{100 * sav / lst:.1f}%" if lst else "—"
    # Net and realized discount are one fact: `net + savings = list`. A separate discount
    # tile crowded the row; the percentage lives in the Net Spend subtitle with the list
    # denominator (a bare % has no base) and the date span. Green only when there is a
    # real discount — otherwise the card is just the net figure.
    net_sub = f"{disc} savings vs. {compact_money(lst)} list"
    net_card: chrome.KpiCard = (
        ("Net Spend", compact_money(net), net_sub, "savings")
        if lst and sav
        else ("Net Spend", compact_money(net), net_sub)
    )
    cards: list[chrome.KpiCard] = [net_card]
    # Only when there are any: a bill with no credits shouldn't carry a "$0" card. Net
    # already includes these (EffectiveCost is post-credit) — the card names the swing so
    # a month that dropped for a one-off discount, not for less usage, reads as one. This
    # was a Redshift-page-only card; every provider's bill can carry a credit, and the
    # home page deliberately excludes credits from its headline and points here for them.
    credits = _credits_total(scope.group, end, sm) if show_credit_kpi else 0.0
    if credits:
        cards.append(
            (
                "Credits & Discounts",
                f"−{compact_money(abs(credits))}",
                "already in net · itemized under Breakdown",
                "decrease",
            )
        )
    # Provider-shaped cards sit after this provider's own bill.
    cards.extend(card for hook in extra_kpis if (card := hook(sm, end)) is not None)
    chrome.kpi_row(cards)


# A run-rate projection is a completed-day mean extended across the month, so its error
# multiplier is roughly (days in month / history_days). At 1 day that's ~30x — one late
# billing export can make a projected chart segment wildly misleading. Three days is the
# smallest window where a single anomalous day cannot much more than triple the estimate.
_RUN_RATE_MIN_DAYS = 3
def _run_rate_row(scope: Scope) -> tuple[date, float, float | None, int] | None:
    """The latest month's run-rate forecast row — ``(month, forecast_cost,
    actual_to_date, history_days)`` — or None if unknowable.

    Used by the monthly chart's partial-month bar segment
    (:func:`_partial_month_remainder`) so it is based on one fitted number.

    Absent from a lake published before the forecast view existed, so the file is
    checked rather than the query being wrapped in a bare except — a real SQL error
    should still surface loudly. Also absent under a narrowed scope: the forecast is
    fitted per provider, so showing it beside narrowed KPIs would project the whole
    account's run rate onto a subset of it.
    """
    if scope.scoped_forecast:
        return _scoped_run_rate_row(scope)
    if not scope.available("spend_forecast_month"):
        return None
    if not gold_view_published(scope.group, "spend_forecast_month"):
        return None
    row = gold_df(
        "SELECT charge_month, forecast_cost, actual_to_date, history_days "
        f'FROM "{scope.group}".spend_forecast_month '
        "WHERE forecast_kind = 'run_rate' ORDER BY charge_month DESC LIMIT 1"
    )
    if row.empty or row["forecast_cost"].iloc[0] is None:
        return None
    actual = row["actual_to_date"].iloc[0]
    forecast_cost = float(row["forecast_cost"].iloc[0])
    actual_cost = None if pd.isna(actual) else float(actual)
    if scope.group == "databricks":
        backing_forecast, backing_actual = _databricks_backing_run_rate(
            pd.Timestamp(row["charge_month"].iloc[0]).date(), int(row["history_days"].iloc[0])
        )
        forecast_cost += backing_forecast
        actual_cost = (actual_cost or 0.0) + backing_actual
    return (
        pd.Timestamp(row["charge_month"].iloc[0]).date(),
        forecast_cost,
        actual_cost,
        int(row["history_days"].iloc[0]),
    )


def _scoped_run_rate_row(scope: Scope) -> tuple[date, float, float | None, int] | None:
    """Completed-day run rate for a narrowed page's own costs, never its whole bill."""
    daily = _scoped_forecast_daily(scope)
    if daily.empty:
        return None
    daily = daily.copy()
    daily["charge_day"] = pd.to_datetime(daily["charge_day"])
    latest_day = daily["charge_day"].max()
    last_complete_day = latest_day - pd.Timedelta(days=1)
    anchor = last_complete_day.to_period("M").to_timestamp()
    completed = daily[
        (daily["charge_day"] <= last_complete_day)
        & (daily["charge_day"].dt.to_period("M").dt.to_timestamp() == anchor)
    ]
    history_days = len(completed)
    if not history_days:
        return None
    actual_to_date = float(
        daily[daily["charge_day"].dt.to_period("M").dt.to_timestamp() == anchor]["net_cost"].sum()
    )
    days_in_month = int((anchor + pd.offsets.MonthEnd(1)).day)
    forecast_cost = round(float(completed["net_cost"].sum()) / history_days * days_in_month, 2)
    return anchor.date(), forecast_cost, actual_to_date, history_days


def _trend(scope: Scope, label: str, start: date, end: date, *, accent: str) -> None:
    # GROUP BY charge_day is load-bearing: spend_trend_daily is one row per
    # (day, service), not per day. Without it px.area gets several points per x and
    # draws a zig-zag that looks like real volatility rather than a bug.
    trend = gold_df(
        f'SELECT charge_day, sum(net_cost) AS net_cost FROM "{scope.group}".spend_trend_daily '
        + scope.where("spend_trend_daily", f"charge_day >= '{start}'", f"charge_day <= '{end}'")
        + " GROUP BY charge_day ORDER BY charge_day"
    )
    if trend.empty:
        _info(f"No daily {label} rows in range.")
        return
    # This series comes only from the Databricks bill. Mapped S3/EC2 infrastructure
    # costs are currently available at monthly granularity, so calling this total
    # Databricks spend would imply a completeness it does not have.
    chrome.panel_title(
        "Daily Databricks usage (DBUs)" if scope.group == "databricks" else "Daily spend"
    )
    fig = px.area(trend, x="charge_day", y="net_cost", labels={"charge_day": "", "net_cost": ""})
    fig.update_traces(line_color=accent, fillcolor=rgba_hex(accent, 0.18))
    chrome.plot(chrome.style_fig(fig, has_legend=False))


#: The forecast series' legend name. Says "projection" in the legend itself, because the
#: legend is the one label that stays on screen while a reader is reading the bars.
FORECAST_SERIES = "Forecast (projection)"


def _forecast_series(scope: Scope) -> tuple[pd.DataFrame, str]:
    """The 3-month trend extrapolation as ``(month, forecast_cost)`` — or a stated reason
    there isn't one, which the caller renders as a caption under the actuals.

    Returns ``(empty, "")`` only when there is nothing to *say*: no published view at all,
    or no forecast rows. The two cases that need words get them —
    ``spend_forecast_month`` NULLs its trend rows until 3 complete months exist (a flat
    hold over less is meaningless, and drawing nothing there looks like a bug), and a
    narrowed page has a forecast fitted over the whole provider, which cannot be shown as
    that page's own.

    Split out of the chart so the forecast can share the actuals' axis: as its own panel it
    had an independent y-scale, which drew a $14K projection taller than a $40K month.
    """
    if scope.scoped_forecast:
        return _scoped_forecast_series(scope)
    if not gold_view_published(scope.group, "spend_forecast_month"):
        return pd.DataFrame(), ""
    if not scope.available("spend_forecast_month"):
        return pd.DataFrame(), (
            "Next 3 months: no forecast at this scope — it is fitted per provider over the "
            f"whole {scope.group.upper()} bill, and `spend_forecast_month` carries no "
            f"{scope.dimension} to narrow it by, so showing it here would project the "
            "whole account's slope onto the subset of it this page covers."
        )
    rows = gold_df(
        "SELECT charge_month, forecast_cost, history_days "
        f'FROM "{scope.group}".spend_forecast_month '
        "WHERE forecast_kind = 'trend' ORDER BY charge_month"
    )
    if rows.empty:
        return pd.DataFrame(), ""
    priced = rows[rows["forecast_cost"].notna()]
    if priced.empty:
        return pd.DataFrame(), (
            "Next 3 months: not enough history to project — need 3 complete months "
            "(the current month never counts). Backfill with "
            "`flashlight ingest --start <date>` (or raise FLASHLIGHT_INGEST_LOOKBACK_DAYS, "
            "default 35) and re-run `flashlight transform`."
        )
    if scope.group == "databricks":
        # The DBU forecast is generated in databricks.spend_forecast_month. Add the
        # identically scoped, AWS-billed infrastructure forecast here so projected bars
        # reconcile to the actual full-footprint stacks beside them.
        priced = priced.copy()
        priced["forecast_cost"] += _databricks_backing_trend_addition(priced)
    return priced.assign(month=pd.to_datetime(priced["charge_month"]).dt.strftime("%Y-%m")), ""


def _scoped_forecast_daily(scope: Scope) -> pd.DataFrame:
    """Daily operating costs at a narrowed scope, used only to fit its projection.

    ``spend_forecast_month`` is intentionally provider-wide. A narrowed page that opts
    in here fits the same trailing-three-complete-month hold directly from its scoped
    daily costs instead of borrowing its provider's forecast.
    """
    return gold_df(
        f'SELECT charge_day, sum(gross_cost) AS net_cost FROM "{scope.group}".spend_trend_daily '
        + scope.where("spend_trend_daily")
        + " GROUP BY charge_day ORDER BY charge_day"
    )


def _scoped_forecast_series(scope: Scope) -> tuple[pd.DataFrame, str]:
    """A scope-native three-month hold, matching GOLD's conservative forecast rules."""
    daily = _scoped_forecast_daily(scope)
    if daily.empty:
        return pd.DataFrame(), ""
    daily = daily.copy()
    daily["charge_day"] = pd.to_datetime(daily["charge_day"])
    last_complete_day = daily["charge_day"].max() - pd.Timedelta(days=1)
    anchor = last_complete_day.to_period("M").to_timestamp()
    complete = daily[daily["charge_day"] <= last_complete_day].copy()
    complete["charge_month"] = complete["charge_day"].dt.to_period("M").dt.to_timestamp()
    history = (
        complete[complete["charge_month"] < anchor]
        .groupby("charge_month", as_index=False)["net_cost"]
        .sum()
        .tail(3)
    )
    if len(history) < 3:
        return pd.DataFrame(), (
            "Next 3 months: not enough Redshift history to project — need 3 complete months "
            "(the current month never counts)."
        )
    forecast_cost = max(0.0, round(float(history["net_cost"].mean()), 2))
    months = [anchor + pd.DateOffset(months=n) for n in range(1, 4)]
    return pd.DataFrame(
        {
            "charge_month": months,
            "forecast_cost": [forecast_cost] * 3,
            "history_days": [int(len(history))] * 3,
            "month": [month.strftime("%Y-%m") for month in months],
        }
    ), ""


# Which providers get their monthly bar split into meaningful cost components rather
# than drawn flat. Databricks uses service names ("Databricks Jobs Compute",
# "Databricks Serverless SQL"); Redshift uses its connector-stamped cost
# subcategories (compute, storage, concurrency scaling, Spectrum). The stack answers
# "why did the month move?" before anyone clicks. Deliberately narrower than
# `_spend_pivot`'s SKU-for-Databricks rule below and *in tension with* it: SKU is the
# right grain for a breakdown table, but dozens of SKUs make an unreadable stack,
# where a handful of services fit the 8-slot palette. Other providers stay flat
# because a credit lands as a negative service row and would render as a segment
# below zero on the one chart that's meant to read as "the bill".
_STACK_BY_SERVICE = frozenset({"databricks", "aws"})


def _service_order(df: pd.DataFrame) -> list[str]:
    """Services biggest-first, with the folded "Other" bucket pinned last — so a
    segment keeps its colour and its position across months instead of shuffling
    with whichever service happened to appear first in the query result."""
    totals = df.groupby("service_name")["net_cost"].sum().sort_values(ascending=False)
    named = [s for s in totals.index if s != chrome.OTHER_SERIES]
    return named + ([chrome.OTHER_SERIES] if chrome.OTHER_SERIES in totals.index else [])


def _monthly_by_service(scope: Scope, end: date, sm: date) -> pd.DataFrame:
    """Monthly net cost split into the clearest scoped components for a stack.

    ``spend_by_service_month`` is the same ``sum(cost)`` over the same
    ``silver.focus_normalized`` as ``monthly_bill``, only grouped finer, so the
    stack's total height still equals the net cost the panel title promises.
    """
    if scope.group not in _STACK_BY_SERVICE:
        return pd.DataFrame()
    # Redshift's service name would collapse compute, storage, concurrency scaling,
    # and Spectrum into one block. Its stamped subcategories are the invoice anatomy
    # the monthly chart is meant to explain.
    if scope.group == "aws" and gold_view_published(scope.group, "spend_by_cost_subcategory_month"):
        df = gold_df(
            f'SELECT charge_month, cost_subcategory AS service_name, sum(net_cost) AS net_cost '
            f'FROM "{scope.group}".spend_by_cost_subcategory_month '
            + scope.where(
                "spend_by_cost_subcategory_month",
                f"charge_month >= '{sm}'",
                f"charge_month <= '{end}'",
            )
            + " GROUP BY charge_month, cost_subcategory ORDER BY charge_month"
        )
        if df.empty:
            return df
        # This is an operating-cost view. ``other`` includes unused commitments and
        # other invoice-accounting lines that cannot honestly be called consumption;
        # those remain in Breakdown, never as a stacked operating-cost segment.
        df = df[~df["service_name"].isin({"other", "serverless"})].copy()
        df = df.groupby(["charge_month", "service_name"], as_index=False)["net_cost"].sum()
        df = df[df["net_cost"].abs() > 0.005]
        return chrome.cap_series(df, "service_name", "net_cost")
    if not gold_view_published(scope.group, "spend_by_service_month"):
        return pd.DataFrame()
    df = gold_df(
        f'SELECT charge_month, service_name, sum(net_cost) AS net_cost FROM "{scope.group}"'
        ".spend_by_service_month "
        + scope.where(
            "spend_by_service_month", f"charge_month >= '{sm}'", f"charge_month <= '{end}'"
        )
        + " GROUP BY charge_month, service_name ORDER BY charge_month"
    )
    return df if df.empty else chrome.cap_series(df, "service_name", "net_cost")


#: Same driver names home_overview.py folds into the Home stack — kept identical so a
#: reader comparing the two pages sees the same label for the same AWS-billed spend.
_DBX_STORAGE_SERVICE = "Databricks Storage"
_DBX_COMPUTE_SERVICE = "Databricks Compute"


def _databricks_backing_monthly(end: date, sm: date) -> pd.DataFrame:
    """Databricks-managed Backing storage + Backing compute, monthly — shaped like
    ``_monthly_by_service``'s own output so it can be concatenated straight onto it.

    Databricks-only (callers gate on ``scope.group == "databricks"``): these are AWS
    bills, not Databricks' own service breakdown, so appearing in this stack is a
    deliberate exception, not something every provider's Trend & changes gets. Net
    Spend (``databricks.monthly_bill``) is unaffected — this only changes what this one
    chart draws, the same "beside, never inside" rule the KPI cards keep (see
    ``databricks_footprint.py``, ``backing_storage.py``, ``backing_compute.py``).
    """
    frames: list[pd.DataFrame] = []
    for group, view, service in (
        ("storage", "backing_storage_month", _DBX_STORAGE_SERVICE),
        ("compute", "backing_compute_month", _DBX_COMPUTE_SERVICE),
    ):
        if not gold_view_published(group, view):
            continue
        df = gold_df(
            f"SELECT charge_month, sum(net_cost) AS net_cost FROM {group}.{view} "
            f"WHERE mapping = 'databricks' AND charge_month >= '{sm}' AND charge_month <= '{end}' "
            "GROUP BY charge_month"
        )
        if df.empty:
            continue
        df["service_name"] = service
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _databricks_backing_run_rate(month: date, history_days: int) -> tuple[float, float]:
    """Forecast the mapped AWS infrastructure alongside a DBU run rate.

    The backing views are monthly, so their accrued current-month total is extended using
    the same completed-day count as the DBU forecast.  This keeps the chart's partial
    actual bar and its hatched remainder on one *full Databricks footprint* basis.
    """
    if history_days <= 0:
        return 0.0, 0.0
    backing = _databricks_backing_monthly(month, month)
    if backing.empty:
        return 0.0, 0.0
    actual = float(backing["net_cost"].sum())
    days_in_month = (pd.Timestamp(month) + pd.offsets.MonthBegin(1) - pd.Timestamp(month)).days
    return round(actual / history_days * days_in_month, 2), actual


def _databricks_backing_trend_addition(forecast: pd.DataFrame) -> float:
    """Trailing-three-month backing-cost mean for a DBU trend forecast.

    A trend row starts in the month after its DBU anchor.  Use only backing months before
    that anchor, matching the DBU forecast's complete-month rule, then add the same
    level forecast to each future month.
    """
    if forecast.empty:
        return 0.0
    first_forecast_month = pd.Timestamp(forecast["charge_month"].min())
    anchor = (first_forecast_month - pd.offsets.MonthBegin(1)).date()
    backing = _databricks_backing_monthly(anchor, date(1900, 1, 1))
    if backing.empty:
        return 0.0
    monthly = (
        backing.assign(charge_month=pd.to_datetime(backing["charge_month"]))
        .loc[lambda df: df["charge_month"] < pd.Timestamp(anchor)]
        .groupby("charge_month")["net_cost"]
        .sum()
        .sort_index()
        .tail(3)
    )
    return float(monthly.mean()) if not monthly.empty else 0.0


def _forecast_marker() -> dict[str, object]:
    """The hatched, cool-slate marker shared by every projected bar segment — a whole
    forecast month or a partial-month remainder alike — so "not yet real" always reads
    the same way. A fresh dict per call: Plotly mutates the figure's marker objects, so
    two traces must never share one.
    """
    return dict(
        color=rgba_hex(chrome.SEMANTIC["forecast"], 0.55),
        pattern=dict(
            shape="/",
            fgcolor=chrome.SEMANTIC["forecast"],
            bgcolor=rgba_hex(chrome.SEMANTIC["forecast"], 0.15),
            size=7,
            solidity=0.35,
        ),
    )


def _partial_month_remainder(scope: Scope, bills: pd.DataFrame) -> tuple[str, float] | None:
    """The still-to-come slice of the current, still-accruing month — or None if it
    shouldn't be drawn.

    ``bills`` already carries that month's actual-to-date as a real (measured) bar; this
    returns ``(month, forecast_cost - actual_to_date)`` so the caller can stack it on top,
    turning a sliver that reads as "the month collapsed" into "realized so far + what the
    run rate says is coming".

    Gated behind the same ``_RUN_RATE_MIN_DAYS`` floor as the KPI card: under 3 days of
    history the run rate multiplies a single day by up to ~30, and stacking that onto the
    chart would draw a bar dwarfing every real month beside it. Also None when the run
    rate's anchor month isn't the latest month in ``bills`` — that happens right after a
    month rolls over and only its first day has landed, so `spend_forecast_month` is still
    anchored on the previous, now-complete month (see ``040_gold_forecast.sql``); that
    barely-started month gets no bar segment at all until it has enough history of its own.
    """
    if bills.empty:
        return None
    row = _run_rate_row(scope)
    if row is None:
        return None
    month, forecast_cost, actual_to_date, history_days = row
    if actual_to_date is None or history_days < _RUN_RATE_MIN_DAYS:
        return None
    label = month.strftime("%Y-%m")
    if label != bills["month"].max():
        return None
    remainder = round(forecast_cost - actual_to_date, 2)
    return (label, remainder) if remainder > 0 else None


def _monthly_drill(
    scope: Scope,
    label: str,
    end: date,
    sm: date,
    *,
    accent: str,
    show_forecast: bool = True,
    show_current_month_projection: bool = True,
    chart_label: str = "Monthly net cost",
) -> None:
    """Monthly bars with the 3-month forecast continuing the same axis to the right.

    One chart, not two stacked panels. Past and projected months are the same quantity on
    the same calendar, so splitting them gave the forecast its own y-scale — which drew a
    $14K projection as a taller bar than a $40K actual month. Sharing the axis makes the
    comparison the reader is already making a correct one.

    What keeps that honest is that the forecast bars must never *look* measured: cool slate
    where the actuals are the palette, hatched where they're solid, named "projection" in
    the legend, and inert on click (there is no measured month behind them to break down).
    Click still opens a per-SKU/service drilldown when it lands on an actual bar.

    The current, still-accruing month is the one exception to "a month is either measured
    or projected, never both": its actual-to-date bar gets a projected remainder stacked
    on top (:func:`_partial_month_remainder`), so a partial month reads as "realized +
    projected" instead of a sliver that looks like spend fell off a cliff.
    """
    bills = _monthly_by_service(scope, end, sm)
    stacked = not bills.empty
    if not stacked:
        bills = _bill_months(scope, sm, end)[["charge_month", "net_cost"]]
    backing = pd.DataFrame()
    if scope.group == "databricks":
        backing = _databricks_backing_monthly(end, sm)
        if not backing.empty:
            if not stacked and not bills.empty:
                # The non-stacked fallback carries no service_name at all (see
                # _bill_months) — give the DBU segment one now that Storage/Compute are
                # about to join it as their own segments, so it doesn't render unlabelled.
                bills = bills.assign(service_name=f"{label} (DBU)")
            # A DBU-only chart with nothing else to stack still needs `stacked=True` once
            # Storage/Compute join it — otherwise the trace-styling branch below treats
            # this as the single-colour, non-stacked case and every segment renders as
            # one unlabelled slice of "accent".
            stacked = True
            bills = pd.concat([bills, backing], ignore_index=True)
    if bills.empty:
        return
    bills["month"] = pd.to_datetime(bills["charge_month"]).dt.strftime("%Y-%m")
    forecast, forecast_note = _forecast_series(scope) if show_forecast else (pd.DataFrame(), "")
    if not forecast.empty:
        # A future month with an actual bar never also gets a forecast bar. barmode is
        # "stack", so the two would pile into one column and read as a total that is part
        # measured and part invented. This happens when the newest data lands on the 1st
        # or 2nd of a month: `spend_forecast_month` fits on complete days only, so its
        # trend projection starts at the previous month.
        forecast = forecast[~forecast["month"].isin(set(bills["month"]))]
        # Bars present ⇒ the legend names them; no prose caveat under the title. Only for
        # the trend itself — the "why no 3-month forecast" note is about the next 3
        # months, a separate question from whether *this* month gets a remainder bar
        # below, so `remainder` never touches it.
        forecast_note = ""
    if show_forecast and show_current_month_projection and scope.scoped_forecast:
        # Redshift accrues in lumpy invoice events, not a stable daily meter. Complete
        # the current bar to the same credit-free trailing-three-month baseline used for
        # its future bars; a daily run rate would turn one large posting into a fiction.
        current_month = bills["month"].max()
        actual_to_date = float(bills.loc[bills["month"] == current_month, "net_cost"].sum())
        baseline = float(forecast["forecast_cost"].iloc[0]) if not forecast.empty else 0.0
        remainder = (
            (current_month, baseline - actual_to_date) if baseline > actual_to_date else None
        )
    else:
        remainder = (
            _partial_month_remainder(scope, bills)
            if show_forecast and show_current_month_projection
            else None
        )
    if not forecast.empty:
        title = f"{chart_label} & 3-month forecast"
    elif remainder is not None:
        title = f"{chart_label} & month-to-date projection"
    else:
        title = chart_label
    chrome.panel_title(title)
    if forecast_note:
        # Only the "why there's no forecast" states (unpublished / not scopable / <3 months).
        chrome.section_caption(forecast_note)
    fig = px.bar(
        bills,
        x="month",
        y="net_cost",
        # A stack needs the palette, not the provider's single accent hue — and its
        # segments are ordered by total spend so the largest service is the same
        # colour in the same place every month (identity, never rank).
        color="service_name" if stacked else None,
        color_discrete_sequence=list(chrome.CATEGORICAL_SLOTS) if stacked else None,
        category_orders={"service_name": _service_order(bills)} if stacked else None,
        barmode="stack",
        custom_data=["month"],
        labels={"month": "", "net_cost": "", "service_name": ""},
    )
    if not stacked:
        fig.update_traces(marker_color=accent, name=f"{label} net", showlegend=True)
    if not forecast.empty:
        fig.add_bar(
            x=list(forecast["month"]),
            y=[float(v) for v in forecast["forecast_cost"]],
            name=FORECAST_SERIES,
            legendgroup="forecast",
            # No custom_data: _on_click reads it to identify the clicked month, and its
            # absence is exactly how a forecast bar declines to be drilled into.
            marker=_forecast_marker(),
            hovertemplate="%{x}<br>forecast $%{y:,.0f}<extra></extra>",
        )
    if remainder is not None:
        remainder_month, remainder_cost = remainder
        fig.add_bar(
            x=[remainder_month],
            y=[remainder_cost],
            name=FORECAST_SERIES,
            legendgroup="forecast",
            # Share one legend entry with the whole-month forecast bars when both are
            # present; stand in for it alone otherwise (<3 complete months ⇒ no trend).
            showlegend=forecast.empty,
            marker=_forecast_marker(),
            # Its own wording: this segment projects only the rest of a month that's
            # already partly real, not the whole month like the trend bars above.
            hovertemplate="%{x}<br>projected remainder $%{y:,.0f}<extra></extra>",
        )
    if not forecast.empty or remainder is not None:
        # An explicit category order: with a categorical x-axis Plotly orders by first
        # appearance per trace, so a forecast month could otherwise land left of an actual.
        # forecast may be the bare-empty DataFrame from _forecast_series (no "month"
        # column at all) when only the remainder segment is present, hence the guard.
        forecast_months = set(forecast["month"]) if not forecast.empty else set()
        fig.update_xaxes(
            categoryorder="array",
            categoryarray=sorted({*bills["month"], *forecast_months}),
        )
    if stacked:
        # A stack answers "what made up the month?"; it can't also answer "how big was
        # the month?" without a reader summing 6-8 thin segments by eye. One text label
        # per bar — the full stack height, actual plus whatever's projected on top of
        # it — answers that directly, without a redundant "Total" legend entry.
        totals: dict[str, float] = bills.groupby("month")["net_cost"].sum().to_dict()
        if not forecast.empty:
            for m, v in zip(forecast["month"], forecast["forecast_cost"], strict=True):
                totals[m] = totals.get(m, 0.0) + float(v)
        if remainder is not None:
            remainder_month, remainder_cost = remainder
            totals[remainder_month] = totals.get(remainder_month, 0.0) + remainder_cost
        for month, total in totals.items():
            fig.add_annotation(
                x=month,
                y=total,
                text=compact_money(total),
                showarrow=False,
                yshift=10,
                font=dict(size=11, color=chrome.INK_SECONDARY),
            )
    # The legend is what tells actuals from projection, so it's on whenever either the
    # stack or a projection is present — not only for the stack.
    has_legend = stacked or not forecast.empty or remainder is not None
    chart = chrome.plot(chrome.style_fig(fig, has_legend=has_legend, category_x=True))

    drill_container = ui.column().classes("w-full gap-4")

    @ui.refreshable
    def drill_body(picked: str | None) -> None:
        drill_container.clear()
        if picked is None:
            return
        with drill_container:
            _drilldown(scope, picked)

    def _on_click(e: GenericEventArguments) -> None:
        points = e.args.get("points") or []
        # A forecast bar carries no customdata, so clicking one is a no-op rather than a
        # KeyError — and rather than a drilldown of a month that hasn't happened.
        custom = points[0].get("customdata") if points else None
        if custom:
            drill_body.refresh(str(custom[0]))

    chart.on("plotly_click", _on_click)


def _drill_movers(scope: Scope, month: date, prior: date) -> pd.DataFrame:
    group = scope.group
    id_col, _, _ = driver_dim(group)
    if group == "databricks":
        return gold_df(
            f"SELECT {id_col}, net_cost, cost_delta, volume_effect, rate_effect, cost_pct_change "
            f"FROM \"{group}\".sku_month_over_month WHERE charge_month = '{month}' "
            "AND cost_delta IS NOT NULL ORDER BY abs(cost_delta) DESC LIMIT 15"
        )
    svc = _service_movers(
        group, month, prior, scope_sql=scope.predicate("spend_by_service_month")
    ).head(15)
    return svc.rename(columns={"k": id_col})


def _drilldown(scope: Scope, month_label: str) -> None:
    """Render the month-over-month breakdown for the clicked month."""
    group = scope.group
    m = pd.Timestamp(f"{month_label}-01")
    prior = m - pd.DateOffset(months=1)

    movers = _drill_movers(scope, m.date(), prior.date())
    decomposed = scope.available("sku_month_over_month")

    if decomposed:
        agg = gold_df(
            "SELECT coalesce(sum(net_cost),0) AS net, coalesce(sum(cost_delta),0) AS delta, "
            "coalesce(sum(volume_effect),0) AS vol, coalesce(sum(rate_effect),0) AS rate, "
            "count(prev_cost) AS comparable "
            f"FROM \"{group}\".sku_month_over_month WHERE charge_month = '{m.date()}'"
        ).iloc[0]
        if int(agg["comparable"]) == 0:
            _info(f"No prior month to compare {m:%b %Y} against — it's the earliest in the data.")
            return
        net, delta, vol, rate = (float(agg[k]) for k in ("net", "delta", "vol", "rate"))
    else:
        # `sku_month_over_month` is per-SKU across the whole provider and carries no
        # service dimension, so at a narrowed scope its totals are the WHOLE account's —
        # reading them here would print the entire AWS bill's delta under a Redshift
        # heading. Derive the headline from this scope's own service movers instead, and
        # say plainly that the volume/rate split isn't available (it exists only in the
        # per-SKU view, which can't be narrowed).
        if movers.empty:
            _info(f"No prior month to compare {m:%b %Y} against — it's the earliest in the data.")
            return
        net = float(movers["net_cost"].sum())
        delta = float(movers["cost_delta"].sum())
        vol = rate = 0.0

    sign = "↑" if delta >= 0 else "↓"
    chrome.panel_title(f"Why did {m:%b %Y} change?")
    caption = (
        f"Net cost {compact_money(net)} · {sign} {compact_money(abs(delta))} vs {prior:%b %Y}. "
    )
    caption += (
        "Volume = usage changed; Rate = price/mix changed (the two sum to the change)."
        if decomposed
        else (
            "No volume/rate split at this scope: that decomposition lives in the per-SKU "
            f"variance view, which carries no {scope.dimension} to narrow by. The movers "
            "below are this page's own services."
        )
    )
    chrome.section_caption(caption)

    def _signed(v: float) -> str:
        return f"{'+' if v >= 0 else '−'}{compact_money(abs(v))}"

    cards: list[chrome.KpiCard] = [
        (f"{m:%b %Y} net", compact_money(net), "after credits"),
        ("Change vs prior", _signed(delta), f"vs {prior:%b}", delta_variant(delta)),
    ]
    if decomposed:
        cards += [
            ("Volume effect", _signed(vol), "usage", "volume"),
            ("Rate effect", _signed(rate), "price / mix", "rate"),
        ]
    chrome.kpi_row(cards)

    if decomposed:
        effects = pd.DataFrame(
            [{"component": "Volume", "effect": vol}, {"component": "Rate", "effect": rate}]
        )
        fig = px.bar(
            effects,
            x="component",
            y="effect",
            color="component",
            color_discrete_map={
                "Volume": chrome.SEMANTIC["volume"],
                "Rate": chrome.SEMANTIC["rate"],
            },
            labels={"component": "", "effect": ""},
        )
        fig.update_layout(showlegend=False)
        for _, r in effects.iterrows():
            fig.add_annotation(
                x=r["component"],
                y=r["effect"],
                text=_signed(float(r["effect"])),
                showarrow=False,
                yshift=10 if r["effect"] >= 0 else -10,
                yanchor="bottom" if r["effect"] >= 0 else "top",
                font=dict(size=11, color=chrome.INK_SECONDARY),
            )
        chrome.panel_title(f"What drove the change · {m:%b %Y}")
        chrome.plot(chrome.style_fig(fig, has_legend=False))

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
                    "Team/project tags are on the Attribution tab — they span all services."
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
            "resource_name": "Resource",
            "resource_type": "Type",
            "sub_account_id": "Workspace",
            "cur": f"{m:%b} net",
            "delta": "Δ vs prior",
            "qty_delta": f"Usage Δ ({unit})",
            "pct": "MoM %",
        },
    )


def _tag_breakdown(group: str, sku: str, m: pd.Timestamp, prior: pd.Timestamp, months: str) -> None:
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


def _short_redshift_sku_name(description: object, net_cost: float) -> str:
    """A readable Redshift billing label; the opaque AWS SKU id remains available.

    AWS's FOCUS ``EffectiveCost`` can include a negative reservation accounting
    line beside the positive ``reserved instance applied`` allocation.  The
    export does not guarantee a human-readable SKU description for that line,
    so calling it "Other Redshift usage" makes an accounting offset look like
    a workload change.  Keep the amount (and thus reconciliation) intact, but
    say what we actually know: it is a negative billing adjustment whose
    precise source remains available through the SKU-ID drill-down/export.
    """
    text = str(description).lower()
    if "reserved instance applied" in text:
        return "Reserved instance applied"
    if "managed storage" in text:
        return "Managed storage"
    if "data scan" in text:
        return "Data scan"
    if "concurrency scaling" in text:
        return "Concurrency scaling"
    if "snapshot storage" in text:
        return "Snapshot storage"
    if "serverless compute" in text:
        return "Serverless compute"
    if net_cost < 0:
        return "Negative billing adjustment"
    return "Other Redshift usage"


def _spend_pivot(scope: Scope, end: date, sm: date, *, include_mom: bool = False) -> None:
    """SKU × month spend matrix, optionally with latest complete-month movement in-line.

    A table can carry the SKU detail a stacked trend chart cannot. When requested by a
    provider-specific page, its delta columns compare the two latest complete months
    *within the selected range*. Redshift now presents that comparison beside its
    cost-subcategory donut instead, while the other providers retain this matrix.
    """
    group = scope.group
    if include_mom or group == "databricks":
        dim, view, label = "sku_id", "spend_by_sku_month", "SKU"
    else:
        dim, view, label = "service_name", "spend_by_service_month", "Service"
    invoice_requested = include_mom and group == "aws"
    invoice_cost = invoice_requested
    # A dashboard process can hot-reload this Python change before its persisted
    # GOLD Parquet has been rebuilt.  Detect that old shape so the page remains
    # usable and tells the operator exactly why it has not switched basis yet.
    invoice_refresh_needed = False
    if invoice_requested:
        columns = gold_df(f'DESCRIBE "{group}".{view}')
        invoice_cost = "billed_cost" in set(columns["column_name"])
        invoice_refresh_needed = not invoice_cost
    cost_column = "billed_cost" if invoice_cost else "net_cost"
    cost_label = "invoice cost" if invoice_cost else "amortized cost"
    chrome.panel_title(f"{label}s × month — {cost_label}")
    if not gold_view_published(group, view):
        chrome.section_caption("Spend by SKU isn't published yet — run `flashlight transform`.")
        return
    name_select = ", arg_max(sku_description, net_cost) AS sku_name" if include_mom else ""
    df = gold_df(
        f"SELECT {dim} AS k, charge_month, sum({cost_column}) AS net_cost{name_select} "
        f'FROM "{group}".{view} '
        + scope.where(view, f"charge_month >= '{sm}'", f"charge_month <= '{end}'")
        + f" GROUP BY {dim}, charge_month"
    )
    if df.empty:
        _info("No SKU rows in range.")
        return

    current = pd.Timestamp(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    pivot = df.pivot_table(
        index="k", columns="charge_month", values="net_cost", aggfunc="sum", fill_value=0.0
    )
    # Drop the uniform ENTERPRISE_ prefix Databricks puts on every SKU id.
    pivot.index = pivot.index.str.replace("ENTERPRISE_", "", regex=False)
    pivot = pivot.reindex(sorted(pivot.columns), axis=1)  # chronological months
    month_columns = list(pivot.columns)
    pivot["Total"] = pivot.sum(axis=1)

    complete_months = (
        [pd.Timestamp(month) for month in month_columns if pd.Timestamp(month) < current]
        if include_mom
        else []
    )
    if len(complete_months) >= 2:
        cmp_month, prior = complete_months[-1], complete_months[-2]
        pivot["Δ vs prior"] = pivot[cmp_month] - pivot[prior]
        pivot["MoM %"] = (100 * pivot["Δ vs prior"] / pivot[prior]).where(pivot[prior] != 0)

    pivot = pivot.sort_values("Total", ascending=False)
    pivot.loc["Total"] = pivot.sum(axis=0)  # column totals reconcile with the row totals
    if len(complete_months) >= 2:
        pivot.loc["Total", "MoM %"] = (
            100 * pivot.loc["Total", "Δ vs prior"] / pivot.loc["Total", prior]
            if pivot.loc["Total", prior]
            else None
        )

    def _col(c: object) -> str:
        if c == "Total":
            return "Total"
        if c in {"Δ vs prior", "MoM %"}:
            return str(c)
        ts = pd.Timestamp(c)
        return f"{ts:%b %Y}" + (" (partial)" if ts == current else "")

    pivot.columns = [
        (f"Δ {cmp_month:%b} vs {prior:%b}" if c == "Δ vs prior" else _col(c)) for c in pivot.columns
    ]
    out = pivot.reset_index()
    rename = {"k": label}
    search_col = "k"
    if include_mom:
        if invoice_refresh_needed:
            chrome.section_caption(
                "Invoice cost is configured, but this lake was published before BilledCost "
                "was added to the SKU view. Run `flashlight transform`, then refresh this page."
            )
        elif invoice_cost:
            chrome.section_caption(
                "Uses AWS invoice cost (BilledCost). Reservation allocations and their "
                "offsets remain available in the amortized-cost data, but do not inflate "
                "this monthly cost view."
            )
        else:
            chrome.section_caption(
                "Uses amortized cost. Negative billing adjustments reconcile reservation/credit "
                "accounting; they are not workload usage. Use the SKU ID or CSV to inspect "
                "the raw line."
            )
        names = (
            df.sort_values("net_cost", ascending=False)
            .drop_duplicates("k")
            .set_index("k")["sku_name"]
        )
        costs = (
            df.groupby("k", as_index=True)["net_cost"].sum().reindex(names.index).fillna(0.0)
        )
        names = pd.Series(
            [
                _short_redshift_sku_name(description, float(cost))
                for description, cost in zip(names.tolist(), costs.tolist(), strict=True)
            ],
            index=names.index,
        )
        out.insert(1, "name", out["k"].map(names).fillna(out["k"]))
        rename = {"name": "SKU", "k": "SKU ID"}
        search_col = "name"
    money_cols = [c for c in out.columns if c not in {"k", "name", "MoM %"}]
    chrome.searchable_table(
        out,
        key=f"{group}_pivot",
        search_col=search_col,
        money_cols=money_cols,
        pct_cols=["MoM %"],
        rename=rename,
        pagination=15,
    )


def _cost_subcategory(
    scope: Scope,
    end: date,
    sm: date,
    *,
    panel_class: str = "",
    embedded: bool = False,
) -> None:
    """Below-SKU cost breakdown, only where a connector stamps ``x_cost_subcategory``
    (Redshift compute/concurrency-scaling/storage/spectrum-scan, and S3
    storage/requests/data-transfer/monitoring/early-delete). Renders nothing for
    services that don't populate it.

    Takes a :class:`Scope`, not a bare ``group``, and that's load-bearing rather than
    tidiness: the view carries ``service_name``, so on ``/aws`` — a Redshift-scoped page
    over a group that also holds S3 — a bare group read would silently grow a second pie
    titled "Amazon Simple Storage Service" under a Redshift heading. It was unscoped only
    while Redshift was the sole ``x_cost_subcategory`` producer.
    """
    where = scope.where(
        "spend_by_cost_subcategory_month",
        f"charge_month >= '{sm}'",
        f"charge_month <= '{end}'",
    )
    df = gold_df(
        "SELECT service_name, cost_subcategory, sum(net_cost) AS net_cost "
        f'FROM "{scope.group}".spend_by_cost_subcategory_month {where} '
        "GROUP BY service_name, cost_subcategory"
    )
    if df.empty:
        return
    def _content() -> None:
        chrome.panel_title("Cost subcategory breakdown")
        with ui.row().classes("w-full gap-4 flex-wrap"):
            for service_name, sub in df.groupby("service_name"):
                with ui.column().classes("gap-0").style("min-width:280px;flex:1;"):
                    ui.label(str(service_name)).classes("text-sm").style(
                        f"color:{chrome.INK_SECONDARY}"
                    )
                    pie = px.pie(sub, names="cost_subcategory", values="net_cost", hole=0.45)
                    pie.update_traces(textposition="inside", textinfo="percent+label")
                    chrome.plot(chrome.style_fig(pie, has_legend=False, currency_axis=None))

    if embedded:
        _content()
        return
    with chrome.panel() as panel:
        if panel_class:
            panel.classes(panel_class)
        _content()


def _credits_df(group: str, end: date, sm: date) -> pd.DataFrame:
    """Credit/adjustment lines for *group* in range, newest month first.

    Empty frame when the group has no credits, or when the lake predates
    ``gold.credits_month`` (published but not re-transformed since) — callers render
    nothing rather than raising, same guard as every other newer-view read here.
    """
    if not gold_view_published(group, "credits_month"):
        return pd.DataFrame()
    return gold_df(
        "SELECT charge_month, charge_description, charge_category, service_name, "
        "sum(net_cost) AS net_cost, sum(line_count) AS line_count "
        f'FROM "{group}".credits_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY charge_month, charge_description, charge_category, service_name "
        "ORDER BY charge_month DESC, net_cost"
    )


def _credits_total(group: str, end: date, sm: date) -> float:
    """Signed (negative) sum of *group*'s credits in range; 0.0 when there are none."""
    df = _credits_df(group, end, sm)
    return float(df["net_cost"].sum()) if not df.empty else 0.0


def _credits(group: str, end: date, sm: date) -> None:
    """Discounts & credits, itemized by credit line.

    The home page deliberately leaves credits out of its headline — a one-off credit
    swings a month without any usage changing — and points here for the detail, so this
    is where they're named rather than just netted. Renders nothing when the bill carries
    none (absence needs no explanation: no credits means no credits).
    """
    df = _credits_df(group, end, sm)
    if df.empty:
        return
    display = df.assign(charge_month=pd.to_datetime(df["charge_month"]).dt.strftime("%b %Y"))
    with chrome.panel():
        chrome.panel_title("Discounts & credits")
        chrome.flat_table(
            display,
            key=f"credits_{group}",
            money_cols=["net_cost"],
            int_cols=["line_count"],
            rename={
                "charge_month": "Month",
                "charge_description": "Credit / adjustment",
                "charge_category": "Category",
                "service_name": "Service",
                "net_cost": "Amount",
                "line_count": "Lines",
            },
        )


def _driver_mom(scope: Scope, end: date, *, sku_mom_scoped: bool = False) -> None:
    group = scope.group
    id_col, id_label, _ = driver_dim(group)
    if sku_mom_scoped:
        id_col, id_label = "sku_id", "SKU"
    chrome.panel_title(f"{id_label} month-over-month")
    # Compare the latest COMPLETE month (exclude the current, still-accruing month) so
    # we never pit a partial month against a full one. Narrowed pages discover that month
    # from their own scoped rows: a month in which only services outside this page's scope
    # billed is not a month this page can compare.
    current = pd.Timestamp(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    month_view = "spend_by_sku_month" if sku_mom_scoped else (
        "sku_month_over_month"
        if scope.available("sku_month_over_month")
        else "spend_by_service_month"
    )
    months = gold_df(
        f'SELECT DISTINCT charge_month FROM "{group}".{month_view} '
        + scope.where(month_view, f"charge_month <= '{end}'", f"charge_month < '{current.date()}'")
        + " ORDER BY charge_month DESC LIMIT 1"
    )
    if months.empty:
        chrome.section_caption("Not enough complete months in range to compare.")
        return
    cmp_month = pd.Timestamp(months.iloc[0]["charge_month"])
    prior = cmp_month - pd.DateOffset(months=1)
    chrome.section_caption(
        f"Top {id_label.lower()}s by net cost · {cmp_month:%b %Y} vs {prior:%b %Y}"
    )
    if sku_mom_scoped:
        rows = gold_df(
            "WITH sku_cost AS ("
            "SELECT sku_id, arg_max(sku_description, net_cost) AS sku_description, "
            f"coalesce(sum(net_cost) FILTER (WHERE charge_month = '{cmp_month.date()}'), 0) "
            "AS net_cost, "
            f"coalesce(sum(net_cost) FILTER (WHERE charge_month = '{prior.date()}'), 0) "
            "AS prev_cost "
            f'FROM "{group}".spend_by_sku_month '
            + scope.where(
                "spend_by_sku_month",
                f"charge_month IN ('{cmp_month.date()}', '{prior.date()}')",
            )
            + " GROUP BY sku_id) "
            "SELECT sku_id, sku_description, net_cost, net_cost - prev_cost AS cost_delta, "
            "CASE WHEN prev_cost <> 0 THEN 100 * (net_cost - prev_cost) / prev_cost END "
            "AS cost_pct_change FROM sku_cost ORDER BY net_cost DESC LIMIT 20"
        )
        if rows.empty:
            _info("No SKU movement rows for the latest complete month.")
            return
        rows.insert(
            0,
            "sku_name",
            rows.apply(
                lambda row: _short_redshift_sku_name(
                    row["sku_description"], float(row["net_cost"])
                ),
                axis=1,
            ),
        )
        chrome.heatmap_table(
            rows[["sku_name", "sku_id", "net_cost", "cost_delta", "cost_pct_change"]],
            heat_col="cost_pct_change",
            key=f"{group}_mom",
            money_cols=["net_cost", "cost_delta"],
            rename={
                "sku_name": "SKU",
                "sku_id": "SKU ID",
                "net_cost": "Net cost",
                "cost_delta": "Δ vs prior",
                "cost_pct_change": "MoM %",
            },
        )
        return
    if group == "databricks":
        mom = gold_df(
            f"SELECT {id_col}, net_cost, cost_delta, cost_pct_change "
            f"FROM \"{group}\".sku_month_over_month WHERE charge_month = '{cmp_month.date()}' "
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
    svc = _service_movers(
        group,
        cmp_month.date(),
        prior.date(),
        scope_sql=scope.predicate("spend_by_service_month"),
    ).head(20)
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
