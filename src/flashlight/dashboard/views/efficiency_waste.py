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
"""

from __future__ import annotations

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.dashboard.theme import compact_money
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


def render(provider_name: str, label: str) -> None:
    """One provider's efficiency tab. *provider_name* is the raw FOCUS ``provider_name``
    (``data.provider_name_for_group``) — never the display label, which matches no row."""
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

    # WASTE and OPPORTUNITY are separate lenses (a cluster can be both — different
    # remedies), so they are shown separately, never summed into one headline.
    chrome.section_caption(f"Showing {month_label} — the latest month with telemetry.")
    chrome.kpi_row(
        [
            (
                "Waste (tune it)",
                compact_money(waste_total),
                "idle · underused",
                "unattributed",
            ),
            ("Opportunity (move)", compact_money(opp_total), "→ jobs compute", "decrease"),
            ("High confidence", compact_money(high_total), "of waste"),
            ("Entities flagged", f"{n_entities:,}", month_label),
        ],
    )

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
