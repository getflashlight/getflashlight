"""Efficiency & waste — one provider's recoverable spend, and what was measured to find it.

The "Efficiency & Waste" tab on every provider page. Reads the one standardized GOLD view
(``efficiency.waste_record``), rendered as a faceted leaderboard: the category IS the cause,
recoverable_cost is the headline, WASTE = tune/right-size it, OPPORTUNITY = move it.

``waste_record`` only carries entities a rule *fired* on, so on its own it can answer "what
is wasteful?" but never "what didn't we look at?". :func:`coverage_caption` answers the
second question from the measurement stage of the same telemetry plane
(``efficiency.utilization_entity_month`` — same ``metrics.efficiency_record`` rows, before
they were judged). It leads the tab because on real data only ~10% of Databricks
entity-months carry a utilization reading and **0%** of AWS's do: absence of a finding is
mostly absence of measurement, and a tab that didn't say so would read as a clean bill of
health. The rest of the measurement stage (per-signal readings, $/native-unit rates) stays in
GOLD for the assistant and MCP rather than being a second set of panels here.

Two later stages of the same pipeline were published to GOLD but never rendered here until
now — a rendering gap, not a missing feature:

* :func:`owner_leaderboard` reads ``efficiency.waste_by_owner_month`` (the "whose?" rollup
  ``056_gold_owner_leaderboard.sql`` builds) — who a WASTE/OPPORTUNITY finding belongs to,
  with the normalized ``(unattributed)`` bucket for shared compute kept visible rather than
  dropped.
* :func:`resolution_panel` reads ``efficiency.waste_resolution_month`` (pure re-detection
  over ``waste_record`` history, no user input) — did a flagged finding stop reappearing,
  and did its billed cost actually drop. This is the only place in the app that answers
  "is this getting fixed?" rather than "what's wrong right now?".

Both are gated behind :func:`~flashlight.dashboard.data.gold_view_published` like every
other view here, so a lake that hasn't re-transformed since these views were added degrades
to nothing rendered, not an error.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd
import plotly.express as px
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.dashboard.theme import compact_money, delta_variant
from flashlight.efficiency.waste_rules import (
    WASTE_RULES,
    WasteRule,
    blocked_rules,
    coverage_groups,
)

_STALE_MSG = (
    "This lake's published GOLD predates the efficiency views — run `flashlight transform` "
    "to rebuild it."
)

# Only entity types where a per-entity reading is obtainable in principle can be
# "unmeasured"; the rest are "not_applicable". Kept here purely for the caption wording —
# the classification itself is baked into the GOLD view (055_gold_utilization.sql).
_NOT_APPLICABLE_REASON = (
    "shared compute, per-user shares, query shapes, tables and serving endpoints"
)


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _df(sql: str) -> pd.DataFrame:
    """Query a waste view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def _empty_state(label: str) -> None:
    """No telemetry at all for this provider — a coverage gap, named, not a hidden tab.

    Every provider page carries this tab, including providers whose connector pulls FOCUS
    cost only (there is no efficiency plane for GCP/Azure/Oracle today). Hiding the tab
    would make "we never measured this" indistinguishable from "there's nothing to find".
    """
    chrome.empty_state(
        "speed",
        f"No efficiency telemetry for {label}",
        "Recoverable-waste detection needs a connector that emits utilization telemetry "
        "alongside cost — today the Databricks connector (job/cluster CPU, warehouse cache "
        f"hit) and the Redshift connector (table, query-shape and WLM metrics). The {label} "
        "connector pulls FOCUS cost only, so there is nothing measured to compare the bill "
        f"against. This is a coverage gap, not a verdict that {label} spend is efficient.",
    )


def coverage_caption(provider_name: str, *, scope_sql: str = "") -> None:
    """One line: how much of this provider's fleet carries a utilization reading at all.

    The surviving panel from the old cross-provider ``/utilization`` page, reduced to its
    load-bearing fact. *scope_sql* is an extra predicate ANDed on (no leading ``AND``) so
    ``redshift_focus`` can state coverage per cluster.
    """
    if not gold_view_published("efficiency", "utilization_entity_month"):
        return
    where = f"WHERE provider_name = '{_q(provider_name)}'" + (
        f" AND {scope_sql}" if scope_sql else ""
    )
    rows = _df(
        "SELECT measurement_status, count(*) AS n, "
        "count(*) FILTER (WHERE is_saturated_reading) AS saturated, "
        f"max(charge_month) AS charge_month FROM efficiency.utilization_entity_month {where} "
        "AND charge_month = (SELECT max(charge_month) FROM "
        f"efficiency.utilization_entity_month {where}) GROUP BY measurement_status"
    )
    if rows.empty:
        chrome.section_caption(
            "No utilization telemetry for this scope, so nothing below is backed by a "
            "measurement of how hard the resource was actually working."
        )
        return

    by_status = rows.set_index("measurement_status")["n"]
    total = int(by_status.sum())
    measured = int(by_status.get("measured", 0))
    not_applicable = int(by_status.get("not_applicable", 0))
    unmeasured = int(by_status.get("unmeasured", 0))
    saturated = int(rows["saturated"].sum())
    month_label = pd.Timestamp(rows["charge_month"].iloc[0]).strftime("%b %Y")

    chrome.section_caption(
        f"Coverage · {month_label}: {measured:,} of {total:,} entity-months carry a "
        f"utilization reading ({100 * measured / total:.0f}%). {not_applicable:,} have "
        f"none obtainable in principle — {_NOT_APPLICABLE_REASON} have no per-entity "
        f"utilization. {unmeasured:,} could be measured but no telemetry arrived. "
        f"{saturated:,} reading(s) are pegged at the sensor ceiling (≥99.5%) — a ceiling, "
        "not a verdict. An entity absent from the tables below is unflagged, not proven "
        "efficient."
    )


def render(provider_name: str, label: str, sm: date, end: date) -> None:
    """One provider's efficiency tab. *provider_name* is the raw FOCUS ``provider_name``
    (``data.provider_name_for_group``) — never the display label, which matches no row.

    *sm*/*end* are the page's own date-range control (same as every other core tab —
    Attribution, Policy Compliance). Only :func:`recoverable_trend_chart` reads them: the
    entity leaderboard below deliberately keeps showing "the latest month with telemetry"
    regardless of the picker (documented where ``month`` is computed) because coverage is
    sparse enough that a narrow window can easily contain zero measured months, and
    :func:`resolution_panel` deliberately shows all-time history — clipping a finding's
    first-seen month to the picker would hide exactly the "has this been open a long time"
    signal it exists to show.
    """
    chrome.section_title("Efficiency & waste")
    chrome.section_caption(
        "Recoverable spend — what you're billed for but don't use. This is the "
        "inefficiency slice, not the total bill."
    )

    if not gold_view_published("efficiency", "waste_record"):
        ui.label(_STALE_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    # No recoverable_cost floor here — waste_record only ever contains rows a rule
    # actually fired for (each branch is `WHERE {where_sql}`), so a $0 row is a real,
    # confirmed finding this provider can't honestly price, not a "nothing found" row.
    # lens_table keeps those visible (sunk to the bottom) instead of dropping them.
    records = _df(
        f"SELECT * FROM efficiency.waste_record WHERE provider_name = '{_q(provider_name)}' "
        "ORDER BY recoverable_cost DESC"
    )
    if records.empty:
        _empty_state(label)
        return

    months = sorted(records["charge_month"].astype(str).unique())
    month = months[-1]
    month_rows = records[records["charge_month"].astype(str) == month]
    month_label = pd.Timestamp(month).strftime("%b %Y")

    coverage_caption(provider_name)

    waste_total = month_rows.loc[month_rows["lens"] == "WASTE", "recoverable_cost"].sum()
    opp_total = month_rows.loc[month_rows["lens"] == "OPPORTUNITY", "recoverable_cost"].sum()
    high_total = month_rows.loc[month_rows["confidence"] == "high", "recoverable_cost"].sum()
    n_entities = month_rows["entity_id"].nunique()
    waste_delta = mom_recoverable_delta(records, months, "WASTE")
    opp_delta = mom_recoverable_delta(records, months, "OPPORTUNITY")

    # WASTE and OPPORTUNITY are separate lenses (a cluster can be both — different
    # remedies), so they are shown separately, never summed into one headline.
    chrome.section_caption(f"Showing {month_label} — the latest month with telemetry.")
    chrome.kpi_row(
        [
            (
                "Waste (tune it)",
                compact_money(waste_total),
                _delta_sub(waste_delta, "idle · underused"),
                delta_variant(waste_delta) if waste_delta is not None else "unattributed",
            ),
            (
                "Opportunity (move)",
                compact_money(opp_total),
                _delta_sub(opp_delta, "→ jobs compute"),
                delta_variant(opp_delta) if opp_delta is not None else "decrease",
            ),
            ("High confidence", compact_money(high_total), "of waste"),
            ("Entities flagged", f"{n_entities:,}", month_label),
        ],
    )

    recoverable_trend_chart(records, sm, end)

    # What was checked, including what found nothing — the counterpart to the lens tables
    # below, which can only ever show rules that fired.
    rule_coverage_table(
        provider_name,
        month_rows,
        measured_entity_types(provider_name, month),
        key=f"rule_coverage_{provider_name.lower().replace(' ', '_')}",
        scope_note=f"for {label}",
    )

    # Split by lens — WASTE and OPPORTUNITY are different remedies and share billed_cost
    # for the same entity, so a single flat list invites double-counting.
    lens_table(month_rows, "WASTE", "Waste — tune or right-size it", "waste")
    lens_table(month_rows, "OPPORTUNITY", "Opportunity — move it to cheaper compute", "opp")

    owner_leaderboard(provider_name, month, month_rows)
    resolution_panel(provider_name)


# ── Month-over-month delta (KPI row) ────────────────────────────────────────────────
def _delta_sub(delta: float | None, fallback: str) -> str:
    """A KPI subtitle: the MoM $ delta when computable, else the static fallback text
    (no history to compare against yet — same "say nothing rather than fabricate a
    delta" rule as ``provider_focus._run_rate_row``)."""
    if delta is None:
        return fallback
    sign = "+" if delta >= 0 else "−"
    return f"{sign}{compact_money(abs(delta))} vs prior month · {fallback}"


def mom_recoverable_delta(
    records: pd.DataFrame, months: list[str], lens: str
) -> float | None:
    """*lens*'s recoverable-$ change, latest measured month vs. the one before it — or
    ``None`` with fewer than two months of history to compare.

    *records*/*months* are the caller's own already-loaded, already-sorted values (the
    same ones that pick ``month`` for the entity leaderboard above), so the delta always
    compares the identical two months a reader sees named elsewhere on the tab rather than
    re-deriving a different pair. More recoverable $ next to last month is a worsening
    trend for either lens — waste growing is bad, and a growing, unaddressed opportunity
    pool is also the bill trending the wrong way — so the sign convention matches
    ``theme.delta_variant``'s cost-delta reading (increase = red, decrease = green)
    unmodified; this is not the "savings" framing :func:`resolution_summary` needs below.
    """
    if len(months) < 2:
        return None
    cur = records.loc[
        (records["charge_month"].astype(str) == months[-1]) & (records["lens"] == lens),
        "recoverable_cost",
    ].sum()
    prior = records.loc[
        (records["charge_month"].astype(str) == months[-2]) & (records["lens"] == lens),
        "recoverable_cost",
    ].sum()
    return round(float(cur) - float(prior), 2)


# ── Trend chart ──────────────────────────────────────────────────────────────────────
def _trend_by_month(records: pd.DataFrame, sm: date, end: date) -> pd.DataFrame:
    """*records* (the caller's full-history rows) narrowed to the page's selected date
    range and rolled up to ``(charge_month, lens)`` — pure aggregation, split out from
    :func:`recoverable_trend_chart` for testing without a NiceGUI context."""
    empty = pd.DataFrame(columns=["charge_month", "lens", "recoverable_cost"])
    if records.empty:
        return empty
    charge_month = pd.to_datetime(records["charge_month"])
    in_range = (charge_month >= pd.Timestamp(sm)) & (charge_month <= pd.Timestamp(end))
    scoped = records.loc[in_range]
    if scoped.empty:
        return empty
    return scoped.groupby(["charge_month", "lens"], as_index=False)["recoverable_cost"].sum()


def recoverable_trend_chart(records: pd.DataFrame, sm: date, end: date) -> None:
    """Recoverable $ by month, split by lens — the trend the KPI row's single-month
    snapshot can't show on its own, over the page's own date-range control (unlike the
    leaderboard/coverage panels above, which deliberately freeze on the latest measured
    month regardless of the picker).

    Bars are grouped, never stacked: stacking WASTE on top of OPPORTUNITY would draw one
    bar height that sums two different remedies into a number neither lens owns — the
    same "never sum across lens" rule the KPI row and :func:`lens_table` already keep.

    Renders nothing with fewer than two measured months in range — a single bar draws no
    trend, and this chart is supplementary context under an already-informative KPI row,
    not load-bearing enough to need its own "why nothing" caption the way
    ``provider_focus._forecast_series`` does.
    """
    trend = _trend_by_month(records, sm, end)
    if trend.empty or trend["charge_month"].nunique() < 2:
        return
    trend = trend.assign(month=pd.to_datetime(trend["charge_month"]).dt.strftime("%Y-%m"))
    with chrome.panel():
        chrome.panel_title("Recoverable $ by month")
        chrome.section_caption(
            "WASTE and OPPORTUNITY plotted separately — never summed into one bar."
        )
        fig = px.bar(
            trend.sort_values("month"),
            x="month",
            y="recoverable_cost",
            color="lens",
            barmode="group",
            color_discrete_map={"WASTE": chrome.WASTE, "OPPORTUNITY": chrome.OPPORTUNITY},
            labels={"month": "", "recoverable_cost": "", "lens": ""},
        )
        chrome.plot(chrome.style_fig(fig, has_legend=True, category_x=True))


# ── Owner attribution rollup ─────────────────────────────────────────────────────────
# "Whose is it?" — the design doc's third pipeline stage (measurement → verdict →
# attribution rollup), published to gold.waste_by_owner_month
# (056_gold_owner_leaderboard.sql) since before this tab existed but never rendered here.
_OWNER_COLS = [
    "owner_display",
    "owner_key",
    "recoverable_cost",
    "recoverable_cost_high_confidence",
    "entity_count",
    "finding_count",
]
_OWNER_RENAME = {
    "owner_display": "Owner",
    "owner_key": "Key",
    "recoverable_cost": "Recoverable",
    "recoverable_cost_high_confidence": "…high confidence",
    "entity_count": "Entities",
    "finding_count": "Findings",
}


@dataclass(frozen=True)
class _OwnerDrill:
    """One position in the owner → entities breadcrumb. A local copy of the same tiny
    value-object shape ``attribution._Drill``/``policy._Drill`` each define for
    themselves — this module follows that established convention (a shared import would
    save a few lines at the cost of coupling three unrelated tabs' navigation state)."""

    level: str = "owners"
    owner_key: str | None = None
    owner_display: str | None = None
    lens: str = "WASTE"


def _owner_breadcrumb(
    *steps: tuple[str, _OwnerDrill | None], refresh: Callable[[_OwnerDrill], object]
) -> None:
    """``"← A / B"`` — every step but the last is a link back to that state."""
    with ui.row().classes("items-center gap-2 text-xs"):
        for i, (label, target) in enumerate(steps):
            if i:
                ui.label("/").style(f"color:{chrome.INK_MUTED}")
            if target is not None:
                ui.button(label, on_click=lambda t=target: refresh(t)).props(
                    "flat dense no-caps"
                ).style(f"color:{chrome.ACCENT};padding:0 2px;min-height:0;")
            else:
                ui.label(label).style(f"color:{chrome.INK_SECONDARY}")


def entities_for_owner(month_rows: pd.DataFrame, owner_key: str, lens: str) -> pd.DataFrame:
    """*month_rows*' own findings for one normalized owner and lens.

    Reuses the leaderboard's already-loaded rows instead of a second SQL round-trip, and
    folds ``owner_user`` the identical way ``056_gold_owner_leaderboard.sql`` does (trim +
    casefold) so a click on the folded key ``"alice"`` matches rows spelled
    ``"Alice"``/``"ALICE "`` on the bill. ``owner_key == "(unattributed)"`` — the view's
    own literal for both its NULL-owner kinds — matches the blank/NULL-owner rows instead;
    scoped to ``owner_dimension='owner_user'`` (see :func:`owner_leaderboard`) there is
    only one such kind in play, so no separate ``owner_kind`` plumbing is needed here.
    """
    rows = month_rows[month_rows["lens"] == lens]
    folded = rows["owner_user"].fillna("").astype(str).str.strip().str.lower()
    if owner_key == "(unattributed)":
        return rows[folded == ""]
    return rows[folded == owner_key]


def _owner_rows(provider_name: str, month: str) -> pd.DataFrame:
    return _df(
        "SELECT owner_key, owner_display, lens, recoverable_cost, "
        "recoverable_cost_high_confidence, entity_count, finding_count "
        "FROM efficiency.waste_by_owner_month "
        f"WHERE provider_name = '{_q(provider_name)}' AND owner_dimension = 'owner_user' "
        f"AND charge_month = '{_q(month)}' ORDER BY recoverable_cost DESC"
    )


def _render_owner_level(
    rows: pd.DataFrame, *, refresh: Callable[[_OwnerDrill], object]
) -> None:
    """Level 1: owners ranked by recoverable $, split by lens (see module docstring for
    why a combined ranking would blur two different remedies together)."""
    chrome.section_caption(
        "'(unattributed)' is shared compute with no per-entity owner by design (a SQL "
        "warehouse or cluster many people query) — not missing data, and often the "
        "largest row. Click an owner to see their own findings."
    )
    for lens, sub_title in (
        ("WASTE", "Waste — tune it"),
        ("OPPORTUNITY", "Opportunity — move it"),
    ):
        lens_rows = rows[rows["lens"] == lens]
        if lens_rows.empty:
            continue
        ui.label(sub_title).classes("text-xs font-medium mt-2").style(
            f"color:{chrome.INK_MUTED}"
        )

        def _on_click(row: dict[str, object], lens: str = lens) -> None:
            refresh(
                _OwnerDrill(
                    level="entities",
                    owner_key=str(row["owner_key"]),
                    owner_display=str(row["owner_display"]),
                    lens=lens,
                )
            )

        cols = [c for c in _OWNER_COLS if c in lens_rows]
        chrome.searchable_table(
            lens_rows[cols],
            key=f"owner_{lens.lower()}",
            search_col="owner_display",
            money_cols=["recoverable_cost", "recoverable_cost_high_confidence"],
            int_cols=["entity_count", "finding_count"],
            rename=_OWNER_RENAME,
            max_rows=_MAX_ROWS,
            on_row_click=_on_click,
        )


def _render_owner_entities(
    month_rows: pd.DataFrame, state: _OwnerDrill, *, refresh: Callable[[_OwnerDrill], object]
) -> None:
    """Level 2: one owner's own findings, breadcrumbed back to the ranking."""
    assert state.owner_key is not None and state.owner_display is not None
    _owner_breadcrumb(
        ("← All owners", _OwnerDrill()),
        (f"{state.owner_display} · {state.lens.title()}", None),
        refresh=refresh,
    )
    rows = entities_for_owner(month_rows, state.owner_key, state.lens)
    if rows.empty:
        chrome.section_caption("No findings for this owner in the latest measured month.")
        return
    rows = rows.assign(remedy=rows["waste_category"].map(_REMEDY_BY_CATEGORY))
    cols = [c for c in _COLS if c in rows and c != "provider_name"]
    chrome.searchable_table(
        rows[cols],
        key=f"owner_entities_{state.owner_key}",
        search_col="entity_name",
        money_cols=["billed_cost", "recoverable_cost"],
        rename=_RENAME,
        max_rows=_MAX_ROWS,
    )


def owner_leaderboard(provider_name: str, month: str, month_rows: pd.DataFrame) -> None:
    """"Whose is it?" — ``efficiency.waste_by_owner_month``, rendered for the first time.

    ``owner_project`` (the view's other ``owner_dimension``) is deliberately not rendered
    here: on real data it's populated on ~1% of findings, so a second, mostly-empty table
    beside this one would repeat the same ``(unattributed)`` row. It stays available to
    MCP/agents that want it (``query_view('efficiency.waste_by_owner_month')``).
    """
    if not gold_view_published("efficiency", "waste_by_owner_month"):
        return
    rows = _owner_rows(provider_name, month)
    if rows.empty:
        return

    with chrome.panel():
        title = (
            ui.label("Recoverable spend by owner")
            .classes("text-sm font-medium mb-2")
            .style(f"color:{chrome.INK_SECONDARY}")
        )
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _OwnerDrill) -> None:
            body.clear()
            title.text = (
                "Recoverable spend by owner"
                if state.level == "owners"
                else f"Recoverable spend by owner — {state.owner_display}"
            )
            with body:
                if state.level == "owners":
                    _render_owner_level(rows, refresh=_body.refresh)
                else:
                    _render_owner_entities(month_rows, state, refresh=_body.refresh)

        _body(_OwnerDrill())


# ── Resolution tracking ──────────────────────────────────────────────────────────────
# "Is flagged waste getting fixed?" — efficiency.waste_resolution_month (pure
# re-detection over gold.waste_record history, no user input) — published alongside
# waste_by_owner_month, also never rendered here until now. The only panel anywhere in
# the app that answers "is this improving" rather than "what's wrong right now".
_OPEN_COLS = [
    "entity_name",
    "entity_type",
    "waste_category",
    "owner_user",
    "first_seen_month",
    "recoverable_cost_at_last_seen",
]
_OPEN_RENAME = {
    "entity_name": "Entity",
    "entity_type": "Type",
    "waste_category": "Cause",
    "owner_user": "Owner",
    "first_seen_month": "Flagged since",
    "recoverable_cost_at_last_seen": "Recoverable",
}
_RESOLVED_COLS = [
    "entity_name",
    "entity_type",
    "waste_category",
    "owner_user",
    "last_seen_month",
    "resolved_month",
    "realized_savings",
]
_RESOLVED_RENAME = {
    "entity_name": "Entity",
    "entity_type": "Type",
    "waste_category": "Cause",
    "owner_user": "Owner",
    "last_seen_month": "Last flagged",
    "resolved_month": "Resolved",
    "realized_savings": "Realized savings",
}


def _resolution_rows(provider_name: str) -> pd.DataFrame:
    return _df(
        "SELECT * FROM efficiency.waste_resolution_month "
        f"WHERE provider_name = '{_q(provider_name)}'"
    )


def resolution_summary(rows: pd.DataFrame) -> dict[str, object]:
    """Pure aggregation behind :func:`resolution_panel` — split out so the resolved /
    still-open / oldest-open arithmetic is directly testable without a NiceGUI context,
    same rationale as :func:`rule_coverage_rows`.

    *rows* is one provider's full ``efficiency.waste_resolution_month`` history (both
    resolved and still-open spans — never date-filtered, see :func:`resolution_panel`).
    ``current_month`` is read off the data itself (the latest ``last_seen_month`` present)
    rather than ``date.today()`` — a lake ingested weeks ago must not compute "3 months
    open" against today's calendar date when its own telemetry stops a month earlier.
    """
    empty: dict[str, object] = {
        "resolved_count": 0,
        "realized_savings_total": 0.0,
        "open_count": 0,
        "open_recoverable_total": 0.0,
        "oldest_open_months": None,
        "current_month": None,
    }
    if rows.empty:
        return empty
    is_resolved = rows["is_resolved"].astype(bool)
    resolved, open_ = rows[is_resolved], rows[~is_resolved]
    current_month = pd.to_datetime(rows["last_seen_month"]).max()
    oldest_open_months = None
    if not open_.empty:
        oldest_first_seen = pd.to_datetime(open_["first_seen_month"]).min()
        # +1: a finding first seen in the current month itself has been open 1 month,
        # not 0 — this counts the flagged month itself, not just the gap since it.
        oldest_open_months = (
            (current_month.year - oldest_first_seen.year) * 12
            + (current_month.month - oldest_first_seen.month)
            + 1
        )
    return {
        "resolved_count": int(len(resolved)),
        "realized_savings_total": float(resolved["realized_savings"].fillna(0).sum()),
        "open_count": int(len(open_)),
        "open_recoverable_total": float(open_["recoverable_cost_at_last_seen"].fillna(0).sum()),
        "oldest_open_months": oldest_open_months,
        "current_month": current_month,
    }


def _realized_savings_variant(realized: float) -> str:
    """Positive realized savings is good news (green); negative — cost rose for other
    reasons after the finding cleared, see :func:`resolution_panel`'s caption — is a
    genuine warning (red), never green just because a finding "resolved". Deliberately
    NOT ``theme.delta_variant``: that helper reads a rising cost as bad, but a rising
    *savings* figure is the opposite — good news, not a worsening trend.
    """
    if realized > 0:
        return "savings"
    if realized < 0:
        return "increase"
    return "neutral"


def resolution_panel(provider_name: str) -> None:
    """"Is flagged waste getting fixed?" — see the module docstring and the section
    banner above for what this reads and why it ignores the page's date range."""
    if not gold_view_published("efficiency", "waste_resolution_month"):
        return
    rows = _resolution_rows(provider_name)
    if rows.empty:
        return
    summary = resolution_summary(rows)
    resolved_count = int(summary["resolved_count"])  # type: ignore[call-overload]
    open_count = int(summary["open_count"])  # type: ignore[call-overload]
    realized = float(summary["realized_savings_total"])  # type: ignore[arg-type]
    open_recoverable = float(summary["open_recoverable_total"])  # type: ignore[arg-type]
    oldest = summary["oldest_open_months"]

    with chrome.panel():
        chrome.panel_title("Resolution tracking")
        chrome.section_caption(
            "Pure re-detection, not a ticketing system: a finding is 'resolved' when it "
            "stopped reappearing in gold.waste_record, and realized savings compares "
            "billed cost the month it was last flagged vs. the month right after (a "
            "terminated entity counts as a full recovery). Can go negative if cost rose "
            "for other reasons after the finding cleared."
        )
        chrome.kpi_row(
            [
                (
                    "Resolved",
                    f"{resolved_count:,}",
                    f"{compact_money(realized)} realized savings, all-time",
                    _realized_savings_variant(realized) if resolved_count else "neutral",
                ),
                (
                    "Still open",
                    f"{open_count:,}",
                    f"{compact_money(open_recoverable)} recoverable"
                    + (f" · oldest flagged {oldest} mo ago" if oldest else ""),
                    "unattributed",
                ),
            ],
        )

        if open_count:
            chrome.panel_title("Still open — oldest flagged first")
            open_rows = rows[~rows["is_resolved"].astype(bool)].sort_values("first_seen_month")
            cols = [c for c in _OPEN_COLS if c in open_rows]
            chrome.searchable_table(
                open_rows[cols],
                key=f"waste_open_{provider_name.lower().replace(' ', '_')}",
                search_col="entity_name",
                money_cols=["recoverable_cost_at_last_seen"],
                rename=_OPEN_RENAME,
                max_rows=_MAX_ROWS,
            )

        if resolved_count:
            chrome.panel_title("Recently resolved")
            resolved_rows = rows[rows["is_resolved"].astype(bool)].sort_values(
                "resolved_month", ascending=False
            )
            cols = [c for c in _RESOLVED_COLS if c in resolved_rows]
            chrome.searchable_table(
                resolved_rows[cols],
                key=f"waste_resolved_{provider_name.lower().replace(' ', '_')}",
                search_col="entity_name",
                money_cols=["realized_savings"],
                rename=_RESOLVED_RENAME,
                max_rows=_MAX_ROWS,
            )


# Displayed in order; descriptor columns that are constant across a lens table collapse
# into the caption instead of repeating on every row (e.g. placement is always
# interactive · candidate · → jobs compute).
_COLS = [
    "entity_name",
    "entity_type",
    "provider_name",
    "owner_user",
    "waste_category",
    "confidence",
    "detail",
    "billed_cost",
    "recoverable_cost",
    "remedy",
]
_CONSTANT_CANDIDATES = [
    "entity_type",
    "provider_name",
    "waste_category",
    "confidence",
    "detail",
    "remedy",
]
_RENAME = {
    "entity_name": "Entity",
    "entity_type": "Type",
    "provider_name": "Provider",
    "owner_user": "Owner",
    "waste_category": "Cause",
    "confidence": "Confidence",
    "detail": "Detail",
    "billed_cost": "Billed",
    "recoverable_cost": "Recoverable",
    "remedy": "How to recover it",
}
# waste_category -> its rule's actionable fix text — waste_record (SQL) has no room for
# free text this long, so it's joined in here from the one place it's authored.
_REMEDY_BY_CATEGORY = {r.category: r.remedy for r in WASTE_RULES}
_MIN_RECOVERABLE = 1.0  # sub-dollar rows are noise (and round to "$0" in the table)
_MAX_ROWS = 40


def lens_table(month_rows: pd.DataFrame, lens: str, title: str, key: str) -> None:
    """One lens's findings table. Public because ``redshift_focus`` renders its own
    per-cluster pair of these — the alternative was a private cross-module import."""
    # Impact-first: rank by recoverable $. Confidence is a column + its own KPI; making it
    # the primary sort buried the big-$ candidate rows under a long tail of tiny high-conf
    # ones. Sub-dollar-but-positive rows are dropped as noise, but an exact-$0 row is kept
    # — that's a rule that fired with no honest $ tie (e.g. Redshift doesn't bill per-table
    # or per-query), a real finding, not nothing found. Sorting by recoverable_cost already
    # sinks those to the bottom instead of losing them.
    cost = month_rows["recoverable_cost"]
    rows = month_rows[
        (month_rows["lens"] == lens) & ((cost >= _MIN_RECOVERABLE) | (cost == 0))
    ].sort_values("recoverable_cost", ascending=False)
    if rows.empty:
        return
    rows = rows.assign(remedy=rows["waste_category"].map(_REMEDY_BY_CATEGORY))
    any_unpriced = bool((rows["recoverable_cost"] == 0).any())

    # Collapse columns that are constant across this table into one context line.
    constants = [
        c for c in _CONSTANT_CANDIDATES if c in rows and rows[c].nunique(dropna=False) == 1
    ]
    context = " · ".join(str(rows[c].iloc[0]) for c in constants)
    cols = [c for c in _COLS if c not in constants]

    with chrome.panel():
        chrome.panel_title(title)
        chrome.section_caption(
            f"Ranked by recoverable $. {('All ' + context + '. ') if context else ''}"
            "underutilized is never shown for shared compute (no per-entity utilization)."
            + (
                " $0 rows are confirmed findings with no honest price tie, not nothing"
                " found — see Detail/How to recover it."
                if any_unpriced
                else ""
            )
        )
        chrome.searchable_table(
            rows[cols],
            key=f"waste_{key}",
            search_col="waste_category",
            money_cols=["billed_cost", "recoverable_cost"],
            rename=_RENAME,
            max_rows=_MAX_ROWS,
        )


# ── Rule coverage ─────────────────────────────────────────────────────────────────
# "What did we even check?" — the counterpart to the lens tables, which can only show
# rules that fired. Lived in redshift_focus, driven by a hand-maintained rule→group map
# that no test kept in sync; it is derived from the pool now (waste_rules.coverage_groups)
# and renders on EVERY provider's tab. Databricks has 23 evaluable rules and had no
# coverage table at all, which made its Efficiency tab the least honest one in the app.
_ENTITY_TYPE_LABELS: dict[str, str] = {
    "sql_warehouse": "Shared SQL compute",
    "sql_warehouse_user": "Per-user",
    "query_pattern": "Per-query-pattern",
    "table": "Per-table",
    "interactive": "All-purpose cluster",
    "job": "Job",
    "notebook": "Notebook",
    "storage": "Storage",
    "": "Any measured entity",
}
# Display order, coarsest scope first. Anything not named here sorts last, alphabetically.
_GROUP_ORDER: tuple[str, ...] = (
    "",
    "interactive",
    "job",
    "notebook",
    "sql_warehouse",
    "sql_warehouse_user",
    "query_pattern",
    "table",
    "storage",
)


def measured_entity_types(
    provider_name: str, month: str, *, scope_sql: str = ""
) -> set[str]:
    """Entity types this provider actually returned telemetry for in *month*.

    The difference between "clean" (we checked, found nothing) and "no data" (the pull
    came back empty) — which is why it reads ``efficiency_entity_month``, every entity
    evaluated, rather than ``waste_record``, only those a rule fired on.
    """
    rows = _df(
        "SELECT DISTINCT entity_type FROM efficiency.efficiency_entity_month "
        f"WHERE provider_name = '{_q(provider_name)}' AND charge_month = '{_q(month)}'"
        + (f" AND {scope_sql}" if scope_sql else "")
    )
    return set(rows["entity_type"]) if not rows.empty else set()


def rule_coverage_rows(
    records: pd.DataFrame,
    measured_types: set[str],
    groups: tuple[tuple[str, tuple[WasteRule, ...]], ...],
) -> list[dict[str, object]]:
    """Pure computation behind the rule-coverage table — every rule evaluable in this
    scope, each resolved to fired (priced or unpriced) / clean / no data. Split out from
    rendering so the fired-vs-clean-vs-no-data logic is directly testable without a
    NiceGUI context.
    """
    by_category = (
        records.groupby("waste_category").agg(
            n=("recoverable_cost", "size"), recoverable_cost=("recoverable_cost", "sum")
        )
        if not records.empty else pd.DataFrame(columns=["n", "recoverable_cost"])
    )
    # The single most-recoverable row's own `detail` text per category — so a fired
    # row can say *what* fired ("$3,458 scanned"), not just how many, without making
    # the caller re-derive it from the lens tables below.
    sample_detail_by_category = (
        records.sort_values("recoverable_cost", ascending=False)
        .groupby("waste_category")["detail"]
        .first()
        if not records.empty and "detail" in records.columns else pd.Series(dtype=object)
    )

    rows: list[dict[str, object]] = []
    for entity_type, rules in groups:
        for rule in rules:
            category = rule.category
            if category in by_category.index:
                n = int(by_category.loc[category, "n"])
                recoverable = float(by_category.loc[category, "recoverable_cost"])
                priced = recoverable > 0
                sample = sample_detail_by_category.get(category) or ""
                status = f"fired · {sample}" if n == 1 and sample else (
                    f"fired · {n} entities" + (f" — e.g. {sample}" if sample else "")
                )
                if not priced:
                    status += " (unpriced)"
            elif entity_type == "" or entity_type in measured_types:
                recoverable, priced, status = 0.0, False, "clean"
            else:
                recoverable, priced, status = 0.0, False, "no data"
            rows.append(
                {
                    # Not rendered (dropped before flat_table) — a stable key for
                    # callers/tests to look a specific rule's row up by, instead of
                    # matching on its prose label.
                    "category": category,
                    "Group": _ENTITY_TYPE_LABELS.get(entity_type, entity_type),
                    "Rule": rule.label,
                    "Lens": rule.lens,
                    "Status": status,
                    "Recoverable": recoverable if priced else float("nan"),
                }
            )
    return rows


def _ordered_groups(
    groups: tuple[tuple[str, tuple[WasteRule, ...]], ...],
) -> tuple[tuple[str, tuple[WasteRule, ...]], ...]:
    def _key(item: tuple[str, tuple[WasteRule, ...]]) -> tuple[int, str]:
        et = item[0]
        return (_GROUP_ORDER.index(et) if et in _GROUP_ORDER else len(_GROUP_ORDER), et)

    return tuple(sorted(groups, key=_key))


def _split_dry_groups(
    groups: tuple[tuple[str, tuple[WasteRule, ...]], ...],
    measured_types: set[str],
    fired: set[str],
) -> tuple[tuple[tuple[str, tuple[WasteRule, ...]], ...], tuple[WasteRule, ...]]:
    """Separate groups worth tabling from ones that only ever produce "no data" rows.

    A whole entity type with no telemetry AND no findings would fill the table with rows
    about a scope that was never in play — S3's storage rule occupying rows on a Redshift
    *cluster*'s table, say. Those collapse into one trailing line so "no data" stays
    visible (it is not the same as clean) without crowding out what was measured.
    """
    kept: list[tuple[str, tuple[WasteRule, ...]]] = []
    dry: list[WasteRule] = []
    for entity_type, rules in groups:
        in_play = entity_type == "" or entity_type in measured_types
        if in_play or any(r.category in fired for r in rules):
            kept.append((entity_type, rules))
        else:
            dry.extend(rules)
    return tuple(kept), tuple(dry)


def status_dot_style(status: str, lens: str) -> tuple[str, str]:
    """(dot CSS, text color) for a rule-coverage Status cell — solid dot = fired &
    priced, hollow = fired but unpriced (or clean), dashed hollow = no data. Color
    follows lens (WASTE red / OPPORTUNITY green); clean/no-data stay muted regardless.
    """
    color = chrome.WASTE if lens == "WASTE" else chrome.OPPORTUNITY
    if status == "clean":
        return f"border:1.5px solid {chrome.INK_MUTED};background:transparent;", chrome.INK_MUTED
    if status == "no data":
        return f"border:1.5px dashed {chrome.INK_MUTED};background:transparent;", chrome.INK_MUTED
    if "(unpriced)" in status:
        return f"border:1.5px solid {color};background:transparent;", color
    return f"background:{color};", color


def rule_coverage_table(
    provider_name: str,
    records: pd.DataFrame,
    measured_types: set[str],
    *,
    key: str,
    scope_note: str = "for this provider",
) -> None:
    """Every rule evaluable in this scope and what it found — including nothing.

    *records* is this scope's ``waste_record`` rows for the month being shown;
    *measured_types* the entity types telemetry actually arrived for.
    """
    groups = _ordered_groups(coverage_groups(provider_name))
    if not groups:
        return
    fired = set(records["waste_category"]) if not records.empty else set()
    groups, dry = _split_dry_groups(groups, measured_types, fired)
    rows = rule_coverage_rows(records, measured_types, groups)
    if not rows:
        return

    with chrome.panel():
        chrome.panel_title("Optimization rule coverage")
        chrome.section_caption(
            f"Every rule evaluated {scope_note} — not only the ones with a dollar figure. "
            "“unpriced” = a real, confirmed finding that can't honestly be priced (Redshift "
            "bills neither per-table nor per-query). “no data” = the telemetry pull came "
            "back empty this window — not the same as clean."
        )
        table = chrome.flat_table(
            pd.DataFrame(rows).drop(columns="category"),
            key=key,
            money_cols=["Recoverable"],
        )
        for row, r in zip(table.rows, rows, strict=True):
            dot_style, color = status_dot_style(str(r["Status"]), str(r["Lens"]))
            row["_status_dot_style"] = dot_style
            row["_status_color"] = color
        table.add_slot(
            "body-cell-Status",
            '<q-td :props="props">'
            '<span style="display:inline-flex;align-items:center;gap:6px;">'
            '<span :style="props.row._status_dot_style" '
            'style="width:8px;height:8px;border-radius:50%;flex:none;"></span>'
            '<span :style="{color: props.row._status_color}">{{ props.value }}</span>'
            "</span></q-td>",
        )
        if dry:
            chrome.section_caption(
                f"{len(dry)} further rule(s) had no telemetry in this scope at all: "
                + ", ".join(r.label for r in dry)
                + ". Not evaluated, so neither clean nor flagged."
            )
        blocked = blocked_rules(provider_name)
        if blocked:
            chrome.section_caption(
                f"{len(blocked)} known pattern(s) are not implemented yet, awaiting "
                "telemetry no connector pulls today: "
                + ", ".join(r.label for r in blocked)
                + ". Listed so 'not built' never reads as 'checked and clean'."
            )
