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
"""

from __future__ import annotations

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.efficiency.policy_config import get_thresholds
from flashlight.efficiency.policy_rules import POLICY_RULES
from flashlight.lake import paths

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
    "remedy",
]
_RENAME = {
    "entity_name": "Entity",
    "entity_type": "Type",
    "owner_user": "Owner",
    "policy_category": "Policy",
    "status": "Status",
    "detail": "Detail",
    "remedy": "How to fix it",
}
# policy_category -> its rule's actionable fix text — policy_record (SQL) has no room
# for free text this long, same join pattern as efficiency_waste.py's remedy column.
_REMEDY_BY_CATEGORY = {r.category: r.remedy for r in POLICY_RULES}
_MAX_ROWS = 40


def _df(sql: str) -> pd.DataFrame:
    """Query the policy view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def render(provider_name: str, label: str) -> None:
    """*provider_name* is the raw FOCUS value (``"AWS"``), never the display label —
    ``policy_record`` rows carry the former, and "AWS Redshift" matches nothing."""
    chrome.section_title(f"{label} policy compliance")
    chrome.section_caption(
        "Are the right cost/attribution guardrails in place — auto-terminate, "
        "autoscaling, cluster policy, tagging? Not a waste signal — no dollar figure."
    )

    if not gold_view_published("policy", "policy_record"):
        ui.label(_STALE_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    records = _df(
        "SELECT * FROM policy.policy_record "
        f"WHERE provider_name = '{provider_name.replace(chr(39), chr(39) * 2)}' "
        "ORDER BY charge_month DESC"
    )
    if records.empty:
        chrome.empty_state(
            "policy",
            f"No policy signals for {label}",
            "Policy compliance is evaluated from the config telemetry a connector "
            f"reports for its entities (cluster auto-termination, warehouse tagging, and "
            f"so on). {label}'s connector doesn't report any yet, so there is nothing to "
            "judge here — that's a coverage gap, not a clean bill of health.",
        )
        return

    months = sorted(records["charge_month"].astype(str).unique())
    month = months[-1]
    month_rows = records[records["charge_month"].astype(str) == month]
    month_label = pd.Timestamp(month).strftime("%b %Y")

    measured = month_rows[month_rows["status"] != "not_applicable"]
    not_evaluated = len(month_rows) - len(measured)
    compliant = int((measured["status"] == "compliant").sum())
    total = len(measured)
    compliance_pct = f"{round(100 * compliant / total)}%" if total else "—"
    non_compliant = total - compliant

    chrome.section_caption(f"Showing {month_label} — the latest month with telemetry.")
    chrome.kpi_row(
        [
            ("Compliant", compliance_pct, f"{compliant:,} of {total:,} measured"),
            ("Non-compliant", f"{non_compliant:,}", month_label),
            # Previously invisible on every provider, Databricks included: cluster_tagging
            # is not_applicable whenever tag_count is NULL, and a compliance percentage
            # over a shrunken denominator reads as a verdict on entities never checked.
            (
                "Not evaluated",
                f"{not_evaluated:,}",
                "no telemetry for the check",
                "unattributed",
            ),
            ("Policies tracked", f"{month_rows['policy_category'].nunique():,}", "categories"),
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
                f"{len(month_rows):,} policy checks apply to {label}'s entities this month "
                f"but none could be evaluated — its connector reports none of the config "
                f"fields they test. Every row is 'not applicable'. This is a telemetry "
                "coverage gap, not compliance."
            )
        _thresholds_panel()
        return

    rows = month_rows[month_rows["status"] == "non_compliant"].assign(
        remedy=month_rows["policy_category"].map(_REMEDY_BY_CATEGORY)
    )
    with chrome.panel():
        chrome.panel_title("Non-compliant entities")
        chrome.section_caption(
            "Ranked by policy category. not_applicable rows (telemetry unmeasured) are "
            "excluded — see the 'Not evaluated' count above for how many."
        )
        if rows.empty:
            ui.label("Nothing non-compliant this month.").classes("text-sm").style(
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

    _thresholds_panel()


def _thresholds_panel() -> None:
    """The numbers behind the pass/fail verdicts, and where to change them.

    Compliance is only meaningful if you can see the threshold it was judged against
    — and edit it when your org's policy differs from Flashlight's default.
    """
    thresholds = get_thresholds()
    with chrome.panel():
        chrome.panel_title("Policy thresholds")
        chrome.section_caption(
            f"The values these verdicts were measured against. Edit {paths.policies_path()} "
            "and re-run `flashlight transform` to change them — thresholds are baked into "
            "the published data, so the dashboard and any agent always agree."
        )
        for name, field in type(thresholds).model_fields.items():
            with ui.row().classes("items-baseline gap-2"):
                ui.label(f"{getattr(thresholds, name)}").classes("text-sm font-medium")
                ui.label(field.description or name).classes("text-sm").style(
                    f"color:{chrome.INK_MUTED}"
                )
