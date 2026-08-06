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
``provider_focus`` — the KPIs and the non-compliant table both sum every entity-month
in the selected window rather than freezing on "the latest month with telemetry", so
narrowing the range up top narrows what's shown here too.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published

_STALE_MSG = (
    "Policy compliance isn't published in this lake yet — run `flashlight transform` "
    "to build it from the telemetry already ingested."
)

_COLS = [
    "entity_name",
    "entity_type",
    "owner_user",
    "policy_category",
    "status",
    "detail",
]
_RENAME = {
    "entity_name": "Entity",
    "entity_type": "Type",
    "owner_user": "Owner",
    "policy_category": "Policy",
    "status": "Status",
    "detail": "Detail",
}
_MAX_ROWS = 40


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _df(sql: str) -> pd.DataFrame:
    """Query the policy view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


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

    measured = records[records["status"] != "not_applicable"]
    not_evaluated = len(records) - len(measured)
    compliant = int((measured["status"] == "compliant").sum())
    total = len(measured)
    compliance_pct = f"{round(100 * compliant / total)}%" if total else "—"
    non_compliant = total - compliant

    chrome.kpi_row(
        [
            ("Compliant", compliance_pct, f"{compliant:,} of {total:,} measured"),
            ("Non-compliant", f"{non_compliant:,}", "in range"),
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

    rows = records[records["status"] == "non_compliant"]
    with chrome.panel():
        chrome.panel_title("Non-compliant entities")
        if rows.empty:
            ui.label("Nothing non-compliant in this range.").classes("text-sm").style(
                f"color:{chrome.INK_MUTED}"
            )
        else:
            chrome.searchable_table(
                rows[_COLS],
                key="policy",
                search_col="policy_category",
                rename=_RENAME,
                max_rows=_MAX_ROWS,
            )
