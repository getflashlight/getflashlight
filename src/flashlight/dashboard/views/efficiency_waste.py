"""Efficiency & waste — one provider's recoverable savings opportunities.

The "Efficiency & Waste" tab on every provider page. It reads the one standardized GOLD view
(``efficiency.waste_record``) and presents one action queue, drilled in place like
Attribution: workload/remedy → entity → evidence and recommended action.

The optional resolution view contributes an entity's all-time first-seen date to the same
drill-down. Both views are gated behind
:func:`~flashlight.dashboard.data.gold_view_published`, so a lake that hasn't
re-transformed since they were added degrades to nothing rendered, not an error.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.summary import (
    action_group_rows,
    action_potential_by_month,
    entity_action_rows,
)
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


def render(provider_name: str, label: str, sm: date, end: date) -> None:
    """One provider's efficiency tab. *provider_name* is the raw FOCUS ``provider_name``
    (``data.provider_name_for_group``) — never the display label, which matches no row.

    The tab deliberately keeps showing the latest completed month regardless of the page's
    date picker. The all-time first-seen date displayed beside an entity is likewise not
    clipped, so it retains the "has this been open a long time" signal.
    """
    with ui.row().classes("items-center gap-2"):
        chrome.section_title("Efficiency & waste")
        ui.label("Metrics below use the previous full month").classes("text-xs").style(
            f"color:{chrome.INK_MUTED}"
        )
    if not gold_view_published("efficiency", "waste_record"):
        ui.label(_STALE_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    # No recoverable_cost floor here — waste_record only ever contains rows a rule
    # actually fired for (each branch is `WHERE {where_sql}`), so a $0 row is a real,
    # confirmed finding this provider can't honestly price, not a "nothing found" row.
    # The action queue retains those rows after drilling into an entity.
    records = _df(
        f"SELECT * FROM efficiency.waste_record WHERE provider_name = '{_q(provider_name)}' "
        "ORDER BY recoverable_cost DESC"
    )
    if records.empty:
        _empty_state(label)
        return

    current_month = _d(
        _df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"]
    )
    months = completed_record_months(records, current_month)
    if not months:
        chrome.empty_state(
            "calendar_month",
            "No completed efficiency month yet",
            "Efficiency findings are shown only after the month closes, so partial-month "
            "costs do not make an optimization opportunity look smaller than it is.",
        )
        return
    month = months[-1]
    month_rows = records[records["charge_month"].astype(str) == month]
    month_label = pd.Timestamp(month).strftime("%b %Y")

    action_groups = _display_action_groups(month_rows)
    savings_total = action_groups["potential_savings"].sum()
    high_total = action_groups["high_confidence"].sum()
    n_entities = month_rows["entity_id"].nunique()
    savings_delta = mom_recoverable_delta(records, months, "ALL")

    chrome.kpi_row(
        [
            (
                "Savings opportunities",
                compact_money(savings_total),
                _delta_sub(savings_delta, "best action per entity"),
                delta_variant(savings_delta) if savings_delta is not None else "unattributed",
            ),
            ("High confidence", compact_money(high_total), "of savings opportunities"),
            ("Entities flagged", f"{n_entities:,}", month_label),
        ],
    )

    # One action queue, drilled in place just like Attribution. Each entity contributes
    # only its best-priced recommendation to the displayed savings total.
    tracking = (
        _resolution_rows(provider_name)
        if gold_view_published("efficiency", "waste_resolution_month")
        else pd.DataFrame()
    )
    action_queue(
        month_rows,
        tracking,
        key=f"actions_{provider_name.lower().replace(' ', '_')}",
    )


def completed_record_months(records: pd.DataFrame, current_month: date) -> list[str]:
    """Available record months strictly before the still-accruing calendar month.

    The action queue is a monthly decision surface, so its snapshot must never use the
    current partial month.  Keeping this as a small pure helper also makes the KPI delta
    and trend cutoff share exactly the same definition of a completed month.
    """
    if records.empty or "charge_month" not in records:
        return []
    cutoff = pd.Timestamp(current_month).replace(day=1)
    record_months = pd.to_datetime(records["charge_month"], errors="coerce").dropna()
    return sorted(record_months[record_months < cutoff].dt.strftime("%Y-%m-%d").unique())


# ── Month-over-month delta (KPI row) ────────────────────────────────────────────────
def _delta_sub(delta: float | None, fallback: str) -> str:
    """A KPI subtitle: the MoM $ delta when computable, else the static fallback text
    (no history to compare against yet — same "say nothing rather than fabricate a
    delta" rule as ``provider_focus._run_rate_row``)."""
    if delta is None:
        return fallback
    sign = "+" if delta >= 0 else "−"
    return f"{sign}{compact_money(abs(delta))} vs prior month · {fallback}"


def mom_recoverable_delta(records: pd.DataFrame, months: list[str], lens: str) -> float | None:
    """*lens*'s recoverable-$ change, latest measured month vs. the one before it — or
    ``None`` with fewer than two months of history to compare.

    *records*/*months* are the caller's own already-loaded, already-sorted values (the
    same ones that pick ``month`` for the entity leaderboard above), so the delta always
    compares the identical two months a reader sees named elsewhere on the tab rather than
    re-deriving a different pair. More recoverable $ next to last month is a worsening
    trend for either lens — waste growing is bad, and a growing, unaddressed opportunity
    pool is also the bill trending the wrong way — so the sign convention matches
    ``theme.delta_variant``'s cost-delta reading (increase = red, decrease = green)
    unmodified.
    """
    if len(months) < 2:
        return None
    potential = action_potential_by_month(
        records,
        pd.Timestamp(months[-2]).date(),
        pd.Timestamp(months[-1]).date(),
        by_lens=lens != "ALL",
    )
    if lens == "ALL":
        cur = potential.loc[
            potential["charge_month"].astype(str) == months[-1], "potential_savings"
        ].sum()
        prior = potential.loc[
            potential["charge_month"].astype(str) == months[-2], "potential_savings"
        ].sum()
        return round(float(cur) - float(prior), 2)
    cur = potential.loc[
        (potential["charge_month"].astype(str) == months[-1]) & (potential["lens"] == lens),
        "potential_savings",
    ].sum()
    prior = potential.loc[
        (potential["charge_month"].astype(str) == months[-2]) & (potential["lens"] == lens),
        "potential_savings",
    ].sum()
    return round(float(cur) - float(prior), 2)


# ── Trend chart ──────────────────────────────────────────────────────────────────────
def _trend_by_month(records: pd.DataFrame, sm: date, end: date) -> pd.DataFrame:
    """Legacy pure aggregation for callers that still need a lens-level monthly rollup."""
    return action_potential_by_month(records, sm, end).rename(
        columns={"potential_savings": "recoverable_cost"}
    )


# ── Savings opportunities ────────────────────────────────────────────────────────────
# One drill-through table, deliberately shaped like Attribution's cost hierarchy.  A
# finding is one rule firing, not necessarily an independent saving: an underutilized
# cluster can also be a placement candidate.  The rollups below therefore take the best
# priced action per entity *within one lens*, while the final level retains every finding.
_ENTITY_LABELS = {
    "job": "Jobs",
    "interactive": "All-purpose clusters",
    "sql_warehouse": "SQL warehouses",
    "sql_warehouse_user": "SQL warehouse users",
    "notebook": "Notebooks",
    "table": "Tables",
    "storage": "Storage",
    "query_pattern": "Query patterns",
    "endpoint": "Serving endpoints",
}
_ACTION_GROUP_COLS = ["workload", "potential_savings", "entities", "high_confidence"]
_ACTION_GROUP_RENAME = {
    "workload": "Savings opportunity",
    "potential_savings": "Potential savings",
    "entities": "Entities",
    "high_confidence": "High confidence",
}
_ACTION_ENTITY_COLS = [
    "entity_name",
    "owner_user",
    "billed_cost",
    "potential_savings",
    "confidence",
    "findings",
    "first_seen_month",
]
_ACTION_ENTITY_RENAME = {
    "entity_name": "Entity",
    "owner_user": "Owner",
    "billed_cost": "Billed",
    "potential_savings": "Potential savings",
    "confidence": "Confidence",
    "findings": "Findings",
    "first_seen_month": "Flagged since",
}
_ACTION_FINDING_COLS = [
    "waste_category",
    "detail",
    "remedy",
    "rule_estimate",
    "potential_savings",
    "confidence",
]
_ACTION_FINDING_RENAME = {
    "waste_category": "Finding",
    "detail": "Evidence",
    "remedy": "Recommended action",
    "rule_estimate": "Rule estimate",
    "potential_savings": "Potential savings",
    "confidence": "Confidence",
}
_REMEDY_BY_CATEGORY = {rule.category: rule.remedy for rule in WASTE_RULES}
_MAX_ROWS = 40


@dataclass(frozen=True)
class _ActionDrill:
    level: str = "workload"
    entity_type: str | None = None
    lens: str | None = None
    entity_id: str | None = None
    entity_name: str | None = None


def _workload_label(entity_type: str) -> str:
    return _ENTITY_LABELS.get(entity_type, entity_type.replace("_", " ").title())


def _display_action_groups(month_rows: pd.DataFrame) -> pd.DataFrame:
    """Shared action potential with the presentation-only workload label added."""
    rows = action_group_rows(month_rows, by_lens=False).copy()
    if rows.empty:
        return pd.DataFrame(columns=_ACTION_GROUP_COLS)
    rows["workload"] = rows.apply(
        lambda row: _workload_label(str(row["entity_type"])), axis=1
    )
    return rows


def _action_breadcrumb(
    *steps: tuple[str, _ActionDrill | None], refresh: Callable[[_ActionDrill], object]
) -> None:
    with ui.row().classes("items-center gap-2 text-xs"):
        for i, (label, target) in enumerate(steps):
            if i:
                ui.label("/").style(f"color:{chrome.INK_MUTED}")
            if target is None:
                ui.label(label).style(f"color:{chrome.INK_SECONDARY}")
            else:
                ui.button(label, on_click=lambda t=target: refresh(t)).props(
                    "flat dense no-caps"
                ).style(f"color:{chrome.ACCENT};padding:0 2px;min-height:0;")


def _add_tracking_context(entities: pd.DataFrame, tracking: pd.DataFrame) -> pd.DataFrame:
    """Attach all-time first-seen context without changing action-potential amounts."""
    if entities.empty or tracking.empty:
        return entities.assign(first_seen_month=pd.NaT)
    keys = ["entity_type", "entity_id", "lens"]
    first_seen = (
        tracking.assign(first_seen_month=pd.to_datetime(tracking["first_seen_month"]))
        .groupby(keys, as_index=False)["first_seen_month"]
        .min()
    )
    return entities.merge(first_seen, how="left", on=keys)


def entity_finding_rows(
    month_rows: pd.DataFrame, entity_type: str, lens: str | None, entity_id: str
) -> pd.DataFrame:
    """Evidence rows whose displayed potential exactly sums to their parent entity.

    Every finding keeps its original rule estimate for auditability. Only the best-priced
    recommendation receives the action-potential amount, because multiple findings on the
    same entity/lane are overlapping signals, not additive savings.
    """
    findings = month_rows[
        (month_rows["entity_type"] == entity_type)
        & (month_rows["entity_id"].astype(str) == entity_id)
    ].copy()
    if lens is not None:
        findings = findings[findings["lens"] == lens]
    if findings.empty:
        return findings.assign(
            rule_estimate=pd.Series(dtype=float), potential_savings=pd.Series(dtype=float)
        )
    findings["rule_estimate"] = pd.to_numeric(
        findings["recoverable_cost"], errors="coerce"
    ).fillna(0)
    findings["potential_savings"] = 0.0
    best_index = findings["rule_estimate"].idxmax()
    findings.loc[best_index, "potential_savings"] = findings.loc[best_index, "rule_estimate"]
    return findings.assign(remedy=findings["waste_category"].map(_REMEDY_BY_CATEGORY))


def action_queue(month_rows: pd.DataFrame, tracking: pd.DataFrame, *, key: str) -> None:
    """One reconcilable work queue: workload/remedy → entity → evidence.

    Each level retains the conservative best-action roll-up.  Individual rule estimates
    are audit evidence, not values that can be summed into a second savings commitment.
    """
    with chrome.panel():
        title = (
            ui.label("Savings opportunities")
            .classes("text-sm font-medium mb-2")
            .style(f"color:{chrome.INK_SECONDARY}")
        )
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _ActionDrill) -> None:
            body.clear()
            with body:
                if state.level == "workload":
                    title.text = "Savings opportunities"
                    chrome.section_caption(
                        "Click through to reconcile the exact potential savings at every level. "
                        "Potential savings uses one best-priced action per entity."
                    )
                    rows = _display_action_groups(month_rows)
                    if rows.empty:
                        chrome.section_caption(
                            "No actionable findings in the latest measured month."
                        )
                        return

                    def _open_workload(row: dict[str, object]) -> None:
                        _body.refresh(
                            _ActionDrill(
                                level="entity",
                                entity_type=str(row["entity_type"]),
                            )
                        )

                    chrome.searchable_table(
                        rows[_ACTION_GROUP_COLS],
                        key=f"{key}_workloads",
                        row_data=rows,
                        search_col="workload",
                        money_cols=["potential_savings", "high_confidence"],
                        int_cols=["entities"],
                        rename=_ACTION_GROUP_RENAME,
                        max_rows=_MAX_ROWS,
                        on_row_click=_open_workload,
                    )
                    return

                assert state.entity_type is not None
                workload = _workload_label(state.entity_type)
                _action_breadcrumb(
                    ("← All opportunities", _ActionDrill()), (workload, None), refresh=_body.refresh
                )
                entities = _add_tracking_context(
                    entity_action_rows(month_rows, state.entity_type), tracking
                )
                if state.level == "entity":
                    title.text = f"Savings opportunities — {workload}"

                    def _open_entity(row: dict[str, object]) -> None:
                        _body.refresh(
                            _ActionDrill(
                                level="finding",
                                entity_type=state.entity_type,
                                entity_id=str(row["entity_id"]),
                                entity_name=str(row["entity_name"]),
                            )
                        )

                    chrome.searchable_table(
                        entities[_ACTION_ENTITY_COLS],
                        key=f"{key}_entities",
                        row_data=entities,
                        search_col="entity_name",
                        money_cols=["billed_cost", "potential_savings"],
                        int_cols=["findings"],
                        rename=_ACTION_ENTITY_RENAME,
                        max_rows=_MAX_ROWS,
                        on_row_click=_open_entity,
                    )
                    return

                assert state.entity_id is not None and state.entity_name is not None
                title.text = f"Savings opportunities — {state.entity_name}"
                _action_breadcrumb(
                    ("← All opportunities", _ActionDrill()),
                    (
                        workload,
                        _ActionDrill(
                            level="entity", entity_type=state.entity_type
                        ),
                    ),
                    (state.entity_name, None),
                    refresh=_body.refresh,
                )
                findings = entity_finding_rows(
                    month_rows, state.entity_type, None, state.entity_id
                )
                chrome.section_caption(
                    "Potential savings reconciles to the entity total: it is assigned to the "
                    "best-priced recommendation. Other rule estimates are supporting evidence."
                )
                chrome.searchable_table(
                    findings[_ACTION_FINDING_COLS],
                    key=f"{key}_findings",
                    search_col="waste_category",
                    money_cols=["rule_estimate", "potential_savings"],
                    rename=_ACTION_FINDING_RENAME,
                    max_rows=_MAX_ROWS,
                )

        _body(_ActionDrill())


def _resolution_rows(provider_name: str) -> pd.DataFrame:
    return _df(
        "SELECT * FROM efficiency.waste_resolution_month "
        f"WHERE provider_name = '{_q(provider_name)}'"
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


def measured_entity_types(provider_name: str, month: str, *, scope_sql: str = "") -> set[str]:
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
        if not records.empty
        else pd.DataFrame(columns=["n", "recoverable_cost"])
    )
    # The single most-recoverable row's own `detail` text per category — so a fired
    # row can say *what* fired ("$3,458 scanned"), not just how many, without making
    # the caller re-derive it from the lens tables below.
    sample_detail_by_category = (
        records.sort_values("recoverable_cost", ascending=False)
        .groupby("waste_category")["detail"]
        .first()
        if not records.empty and "detail" in records.columns
        else pd.Series(dtype=object)
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
                status = (
                    f"fired · {sample}"
                    if n == 1 and sample
                    else (f"fired · {n} entities" + (f" — e.g. {sample}" if sample else ""))
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


def coverage_summary(
    provider_name: str,
    records: pd.DataFrame,
    measured_types: set[str],
    *,
    scope_note: str,
) -> None:
    """Compact diagnostic counterpart to the action queue, deliberately not a table."""
    groups = _ordered_groups(coverage_groups(provider_name))
    if not groups:
        return
    fired = set(records["waste_category"]) if not records.empty else set()
    groups, dry = _split_dry_groups(groups, measured_types, fired)
    rows = rule_coverage_rows(records, measured_types, groups)
    if not rows:
        return
    triggered = sum(str(row["Status"]).startswith("fired") for row in rows)
    clean = sum(row["Status"] == "clean" for row in rows)
    no_data = sum(row["Status"] == "no data" for row in rows) + len(dry)
    blocked = len(blocked_rules(provider_name))
    chrome.section_caption(
        f"Detection coverage {scope_note} · {triggered} triggered · {clean} clean · "
        f"{no_data} without telemetry"
        + (f" · {blocked} known patterns not implemented" if blocked else "")
        + ". Unpriced findings remain available after drilling into an entity."
    )


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
