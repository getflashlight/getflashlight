"""Policy compliance — cost guardrails and attribution tagging, not waste.

Reads the one GOLD view (``policy.policy_record``): one row per (entity, month,
policy_category), status compliant/non_compliant/not_applicable. No dollar figure —
see ``efficiency_waste.py`` for recoverable spend.
"""

from __future__ import annotations

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df
from flashlight.efficiency.policy_config import get_thresholds
from flashlight.efficiency.policy_rules import POLICY_RULES
from flashlight.lake import paths

_EMPTY_MSG = (
    "No policy-compliance data yet. This view needs the Databricks system-table pull "
    "(cluster/warehouse config) — run `flashlight ingest` with a Databricks connector "
    "configured."
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


def render() -> None:
    chrome.section_title("Policy compliance")
    chrome.section_caption(
        "Are the right cost/attribution guardrails in place — auto-terminate, "
        "autoscaling, cluster policy, tagging? Not a waste signal — no dollar figure."
    )

    records = _df("SELECT * FROM policy.policy_record ORDER BY charge_month DESC")
    if records.empty:
        ui.label(_EMPTY_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    months = sorted(records["charge_month"].astype(str).unique())
    month = months[-1]
    month_rows = records[records["charge_month"].astype(str) == month]
    month_label = pd.Timestamp(month).strftime("%b %Y")

    measured = month_rows[month_rows["status"] != "not_applicable"]
    compliant = int((measured["status"] == "compliant").sum())
    total = len(measured)
    compliance_pct = f"{round(100 * compliant / total)}%" if total else "—"
    non_compliant = total - compliant

    chrome.section_caption(f"Showing {month_label} — the latest month with telemetry.")
    chrome.kpi_row(
        [
            ("Compliant", compliance_pct, f"{compliant:,} of {total:,} measured"),
            ("Non-compliant", f"{non_compliant:,}", month_label),
            ("Policies tracked", f"{month_rows['policy_category'].nunique():,}", "categories"),
        ],
    )

    rows = month_rows[month_rows["status"] == "non_compliant"].assign(
        remedy=month_rows["policy_category"].map(_REMEDY_BY_CATEGORY)
    )
    with chrome.panel():
        chrome.panel_title("Non-compliant entities")
        chrome.section_caption(
            "Ranked by policy category. not_applicable rows (telemetry unmeasured) are excluded."
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
