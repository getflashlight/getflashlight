"""Policy compliance — cost guardrails and attribution tagging, not waste.

Reads the one GOLD view (``policy.policy_record``): one row per (entity, month,
policy_category), status compliant/non_compliant/not_applicable. No dollar figure —
see ``efficiency_waste.py`` for recoverable spend.

A **core tab on every provider page**, filtered to that provider — it used to be a
Databricks-only extra tab, which hid real rows: ``policy_record`` is generated from
``metrics.efficiency_record`` with no provider filter (``efficiency/policy_rules.py``),
and two of its rules key on ``entity_type = 'sql_warehouse'``, which the Redshift
connector emits. So every Redshift cluster-month has been contributing rows to this view
all along with nowhere to display them. Providers with no rows get a named empty state
rather than a hidden tab, for the same reason Efficiency & Waste does: "never measured"
must not look like "nothing to find".

Driven by the page's own date range (*end*/*sm*), same as every other tab on
``provider_focus``, rather than freezing on "the latest month with telemetry" — but
unlike a spend view, it does NOT sum every entity-month in the window: ``policy_record``
is one row per (entity, month, policy_category), so a cluster non-compliant for 6
straight months would otherwise repeat as 6 identical-looking rows, which reads as
duplicate data rather than 6 distinct findings (see :func:`_latest_per_entity`). Every
KPI and table below is one row per entity per policy — its most recent evaluation within
the selected window — so narrowing the range still changes what's shown, just without
inflating the count by how many months of history exist.

"Non-compliant entities" is a two-level drill-through, same pattern as Attribution's
"Untagged infrastructure" (``views/attribution.py``): it opens at the *policy* grain —
one row per policy_category, how many entities failed it — because "which policy is
worst" is the question a reader asks before "which specific entity". Clicking a policy
drills into the entities that failed it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.efficiency.policy_rules import POLICY_RULES

_STALE_MSG = (
    "Policy compliance isn't published in this lake yet — run `flashlight transform` "
    "to build it from the telemetry already ingested."
)

# policy_category -> its rule's human label ("Auto-termination policy") — the policy-
# grain table reads better than the raw snake_case category, falls back to it if a
# category has no matching rule (shouldn't happen; policy_record is generated from
# this same pool, but a stale lake could carry a retired category).
_CATEGORY_LABEL = {r.category: r.label for r in POLICY_RULES}
# The reverse, for the policy-grain table's row click: the table displays policy_label
# (not the raw category the DataFrame passed to `chrome.searchable_table` never carries
# — only `_POLICY_COLS` do), so recovering which policy was clicked has to go through
# the label. Safe because every rule in the pool has a distinct label.
_LABEL_TO_CATEGORY = {v: k for k, v in _CATEGORY_LABEL.items()}

_ENTITY_COLS = ["entity_name", "entity_id", "entity_type", "owner_user", "detail"]
_ENTITY_RENAME = {
    "entity_name": "Entity",
    "entity_id": "ID",
    "entity_type": "Type",
    "owner_user": "Owner",
    "detail": "Detail",
}
_POLICY_COLS = ["policy_label", "non_compliant", "compliant", "not_evaluated", "compliance_pct"]
_POLICY_RENAME = {
    "policy_label": "Policy",
    "non_compliant": "Non-compliant",
    "compliant": "Compliant",
    "not_evaluated": "Not evaluated",
    "compliance_pct": "Compliance %",
}
_MAX_ROWS = 40


def _category_label(category: str) -> str:
    return _CATEGORY_LABEL.get(category, category)


def _category_from_label(label: str) -> str:
    """The reverse of :func:`_category_label` — a policy-grain table row only carries
    the displayed label, never the raw ``policy_category`` (see ``_LABEL_TO_CATEGORY``).
    Falls back to the label itself, which is exactly right for an unmapped category:
    :func:`_category_label` returns the raw category unchanged when it has no rule, so
    the label IS the category in that case."""
    return _LABEL_TO_CATEGORY.get(label, label)


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _df(sql: str) -> pd.DataFrame:
    """Query the policy view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def _latest_per_entity(records: pd.DataFrame) -> pd.DataFrame:
    """Collapse to one row per (entity_id, policy_category) — its most recent
    evaluation within the selected range.

    ``policy_record`` is one row per (entity, month, policy_category); showing every
    month verbatim would repeat the same cluster once per month it has been
    non-compliant — a cluster misconfigured for 6 straight months is one problem, not
    six identical-looking rows. Grouping on ``entity_id`` rather than ``entity_name``:
    the latter isn't guaranteed unique (see ``efficiency/model.py``), and it's the
    entity the SQL rules are actually keyed on.
    """
    idx = records.groupby(["entity_id", "policy_category"])["charge_month"].idxmax()
    return records.loc[idx]


@dataclass(frozen=True)
class _Drill:
    """One position in the policy → entities breadcrumb (see :func:`_non_compliant_panel`)."""

    level: str = "policy"
    category: str | None = None


def _breadcrumb(*steps: tuple[str, _Drill | None], refresh: Callable[[_Drill], object]) -> None:
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


def _policy_summary(records: pd.DataFrame) -> pd.DataFrame:
    """One row per policy_category: how many entities are non-compliant/compliant/
    not evaluated, out of every entity this rule applies to in the selected range."""
    rows = []
    for category, g in records.groupby("policy_category"):
        measured = g[g["status"] != "not_applicable"]
        n_measured = len(measured)
        n_compliant = int((measured["status"] == "compliant").sum())
        rows.append(
            {
                "policy_category": category,
                "policy_label": _category_label(str(category)),
                "non_compliant": n_measured - n_compliant,
                "compliant": n_compliant,
                "not_evaluated": len(g) - n_measured,
                "compliance_pct": (100 * n_compliant / n_measured) if n_measured else float("nan"),
            }
        )
    return pd.DataFrame(rows).sort_values("non_compliant", ascending=False)


def _render_policy_level(records: pd.DataFrame, *, refresh: Callable[[_Drill], object]) -> None:
    """Level 1: one row per policy, ranked by how many entities failed it."""
    summary = _policy_summary(records)
    chrome.section_caption("Click a policy to see the entities behind it.")

    def _on_click(row: dict[str, object]) -> None:
        refresh(_Drill(level="entities", category=_category_from_label(str(row["policy_label"]))))

    chrome.searchable_table(
        summary[_POLICY_COLS],
        key="policy_summary",
        search_col="policy_label",
        int_cols=["non_compliant", "compliant", "not_evaluated"],
        pct_cols=["compliance_pct"],
        rename=_POLICY_RENAME,
        max_rows=_MAX_ROWS,
        on_row_click=_on_click,
    )


def _render_entity_level(
    records: pd.DataFrame, category: str, *, refresh: Callable[[_Drill], object]
) -> None:
    """Level 2: the entities that failed one policy.

    Includes ``entity_id`` alongside ``entity_name`` and sorts by name — Databricks
    entity names are frequently a boilerplate template (e.g. many distinct ephemeral
    clusters all named "shared cluster (15.4.x-scala2.12)" after their runtime
    version), so a list of only names looks like duplicate rows even though every
    ``entity_id`` behind them is a different resource.
    """
    _breadcrumb(
        ("← All policies", _Drill()),
        (_category_label(category), None),
        refresh=refresh,
    )
    rows = records[
        (records["policy_category"] == category) & (records["status"] == "non_compliant")
    ].sort_values(["entity_name", "entity_id"])
    if rows.empty:
        ui.label("Nothing non-compliant for this policy in range.").classes("text-sm").style(
            f"color:{chrome.INK_MUTED}"
        )
        return
    chrome.searchable_table(
        rows[_ENTITY_COLS],
        key=f"policy_entities_{category}",
        search_col="entity_name",
        rename=_ENTITY_RENAME,
        max_rows=_MAX_ROWS,
    )


def _non_compliant_panel(records: pd.DataFrame) -> None:
    """The "Non-compliant entities" drill-through panel — policy grain first, entities
    on click, same pattern as Attribution's "Untagged infrastructure" drill-through."""
    with chrome.panel():
        title = ui.label("Non-compliant entities").classes("text-sm font-medium mb-2").style(
            f"color:{chrome.INK_SECONDARY}"
        )
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _Drill) -> None:
            body.clear()
            title.text = (
                "Non-compliant entities"
                if state.level == "policy"
                else f"Non-compliant entities — {_category_label(state.category or '')}"
            )
            with body:
                if state.level == "policy":
                    _render_policy_level(records, refresh=_body.refresh)
                else:
                    assert state.category is not None
                    _render_entity_level(records, state.category, refresh=_body.refresh)

        _body(_Drill())


def render(provider_name: str, label: str, end: date, sm: date) -> None:
    """*provider_name* is the raw FOCUS value (``"AWS"``), never the display label —
    ``policy_record`` rows carry the former, and "AWS Redshift" matches nothing.

    *end*/*sm* are the page's own date range — every number below is scoped to it.
    """
    chrome.section_title(f"{label} policy compliance")

    if not gold_view_published("policy", "policy_record"):
        ui.label(_STALE_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    records = _df(
        "SELECT * FROM policy.policy_record "
        f"WHERE provider_name = '{_q(provider_name)}' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    if records.empty:
        chrome.empty_state(
            "policy",
            f"No policy signals for {label}",
            "Policy compliance is evaluated from the config telemetry a connector "
            f"reports for its entities (cluster auto-termination, warehouse tagging, and "
            f"so on). {label}'s connector doesn't report any yet in the selected range, "
            "so there is nothing to judge here — that's a coverage gap, not a clean bill "
            "of health.",
        )
        return

    records = _latest_per_entity(records)
    measured = records[records["status"] != "not_applicable"]
    not_evaluated = len(records) - len(measured)
    compliant = int((measured["status"] == "compliant").sum())
    total = len(measured)
    compliance_pct = f"{round(100 * compliant / total)}%" if total else "—"
    non_compliant = total - compliant

    chrome.kpi_row(
        [
            ("Compliant", compliance_pct, f"{compliant:,} of {total:,} measured"),
            ("Non-compliant", f"{non_compliant:,}", "distinct entities"),
            # Previously invisible on every provider, Databricks included: cluster_tagging
            # is not_applicable whenever tag_count is NULL, and a compliance percentage
            # over a shrunken denominator reads as a verdict on entities never checked.
            (
                "Not evaluated",
                f"{not_evaluated:,}",
                "no telemetry for the check",
                "unattributed",
            ),
            ("Policies tracked", f"{records['policy_category'].nunique():,}", "categories"),
        ],
    )

    if total == 0:
        # Rows exist but not one was evaluable. Without saying so, the KPIs above read
        # "— compliant · 0 non-compliant" — indistinguishable from a clean bill of health.
        # This is exactly Redshift's case: the connector emits sql_warehouse entities, so
        # the warehouse_tagging/warehouse_auto_stop rules match them, but it reports
        # neither tag counts nor auto-stop timeouts, so every row is not_applicable.
        with chrome.panel():
            chrome.panel_title("Nothing could be evaluated")
            chrome.section_caption(
                f"{len(records):,} policy checks apply to {label}'s entities in this range "
                f"but none could be evaluated — its connector reports none of the config "
                f"fields they test. Every row is 'not applicable'. This is a telemetry "
                "coverage gap, not compliance."
            )
        return

    _non_compliant_panel(records)
