"""Attribution — what one provider's spend is tagged to.

The "Attribution" tab on every provider page (``provider_focus``, ``redshift_focus``).
Panels, top to bottom:

* **Untagged by service** — *where* tagging is missing
  (``<group>.spend_untagged_by_service_month``). Click a service to open the work queue.
* **Untagged resources** — *what to open and tag*
  (``<group>.spend_untagged_by_resource_month``), ranked under the selected service; dollars
  reconcile to that service's untagged total. Remedy text is generic bill-tag guidance,
  overridden by Policy's tagging remedy when ``resource_id`` matches a non-compliant
  cluster/warehouse/endpoint row.
* **Spend by tag key** / **Spend by tag value** — what *is* tagged.

``spend_tag_coverage_month`` remains the provider-level denominator for agents. Policy
Compliance still owns entity-level tagging rules; this tab does not send users *only*
there — many bill gaps have no Policy row.

Per-tag-key spend is never totalled (a resource with two tags is counted under both —
the honest denominator is ``spend_tag_coverage_month.tagged_cost``). Methodology belongs
behind :func:`chrome.info_icon`, not a standing caption.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published, provider_name_for_group
from flashlight.dashboard.theme import compact_money
from flashlight.efficiency.policy_rules import POLICY_RULES

_TAG_COLS = ["tag_key_normalized", "tag_key_variants", "net_cost", "tag_value_count"]
_TAG_RENAME = {
    "tag_key_normalized": "Tag key",
    "tag_key_variants": "Spelled as",
    "net_cost": "Spend",
    "tag_value_count": "Values",
}

_UNTAGGED_COLS = ["service_name", "untagged_cost", "gross_cost", "tagged_pct"]
_UNTAGGED_RENAME = {
    "service_name": "Service",
    "untagged_cost": "Untagged",
    "gross_cost": "Charges",
    "tagged_pct": "Tagged %",
}

_RESOURCE_COLS = [
    "resource_name",
    "resource_id",
    "resource_type",
    "sub_account_id",
    "region_id",
    "untagged_cost",
    "remedy",
]
_RESOURCE_RENAME = {
    "resource_name": "Resource",
    "resource_id": "Id",
    "resource_type": "Type",
    "sub_account_id": "Workspace",
    "region_id": "Region",
    "untagged_cost": "Untagged",
    "remedy": "How to fix it",
}

_MAX_ROWS = 40

_TAGGING_POLICY_CATEGORIES = frozenset(
    {"cluster_tagging", "warehouse_tagging", "endpoint_tagging"}
)
_REMEDY_BY_CATEGORY = {
    r.category: r.remedy for r in POLICY_RULES if r.category in _TAGGING_POLICY_CATEGORIES
}

_GENERIC_REMEDY = (
    "Add cost-allocation tags (e.g. team, project, environment) on this resource in the "
    "cloud console or as code so its spend can be attributed."
)

_TAG_KEY_INFO = (
    "Tagged charges only; don't sum Spend (multi-tagged resources count twice). "
    "Case/separator variants fold into one row — Spelled as lists the raw forms."
)

_UNTAGGED_INFO = (
    "Charges with no cost-allocation tag, by service. Credits excluded. "
    "Tagged % is of that service's charges in range — not of the whole bill. "
    "Click a service to list the untagged resources to fix."
)

_RESOURCE_INFO = (
    "Untagged charges for the selected service, ranked by $. "
    "Add cost-allocation tags on these resources. When a resource matches a Policy "
    "tagging finding, that rule's remedy is shown; otherwise the generic bill-tag fix."
)


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _df(sql: str) -> pd.DataFrame:
    """Query an attribution view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def _info(text: str) -> None:
    ui.label(text).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


def render(group: str, end: date, sm: date) -> None:
    """The whole "Attribution" tab body for one provider.

    Draws its own panels — callers must NOT wrap this in ``chrome.panel()`` (the same
    convention ``provider_focus``'s ``extra_tabs`` follow).
    """
    untagged_by_service(group, end, sm)
    rows = _tag_key_rows(group, end, sm)
    if rows.empty:
        _no_tagged_spend_panel("Spend by tag key — no tagged spend")
        return
    tag_keys(group, end, sm, rows=rows)
    tag_values(group, end, sm, rows=rows)


def untagged_by_service(
    group: str, end: date, sm: date, *, scope_sql: str = ""
) -> None:
    """Services with untagged charges — KPI row + ranked table + resource drill.

    *scope_sql* is an optional extra predicate (no leading AND/WHERE), used by
    ``redshift_focus`` to narrow to Redshift service names. Empty = whole provider.
    """
    if not gold_view_published(group, "spend_untagged_by_service_month"):
        with chrome.panel():
            chrome.panel_title("Untagged by service")
            chrome.section_caption(
                "Untagged-by-service isn't published yet — run `flashlight transform`."
            )
        return

    extra = f" AND {scope_sql}" if scope_sql else ""
    rows = _df(
        "SELECT service_name, "
        "sum(gross_cost) AS gross_cost, "
        "sum(tagged_cost) AS tagged_cost, "
        "sum(untagged_cost) AS untagged_cost "
        f'FROM "{group}".spend_untagged_by_service_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'{extra} "
        "GROUP BY service_name"
    )
    drill_host = ui.column().classes("w-full gap-2")

    @ui.refreshable
    def _open_resources(service: str) -> None:
        drill_host.clear()
        with drill_host:
            untagged_resources(group, service, end, sm, scope_sql=scope_sql)

    with chrome.panel():
        with ui.row().classes("items-center gap-1"):
            chrome.panel_title("Untagged by service")
            chrome.info_icon(_UNTAGGED_INFO)

        if rows.empty:
            chrome.section_caption("No charges in range to measure tag coverage against.")
            return

        # Strict gap filter — exclude floating $0 / fully-tagged noise.
        gaps = rows.loc[rows["untagged_cost"] > 0].copy()
        gross = float(rows["gross_cost"].sum())
        untagged = float(gaps["untagged_cost"].sum()) if not gaps.empty else 0.0
        n_gap = len(gaps)
        share = f"{100 * untagged / gross:.0f}% of charges" if gross > 0 else "—"

        chrome.stat_row(
            [
                ("Untagged", compact_money(untagged), share, "unattributed"),
                (
                    "Services with gaps",
                    f"{n_gap:,}",
                    f"of {len(rows):,} with charges",
                ),
            ]
        )

        if gaps.empty:
            chrome.section_caption("Every service in range carries at least one tag.")
            return

        gaps = gaps.assign(
            tagged_pct=(100 * gaps["tagged_cost"] / gaps["gross_cost"]).where(
                gaps["gross_cost"] > 0
            )
        ).sort_values("untagged_cost", ascending=False)
        cols = [c for c in _UNTAGGED_COLS if c in gaps]
        chrome.section_caption("Click a service to list its untagged resources.")

        def _on_service_click(row: dict[str, object]) -> None:
            _open_resources.refresh(str(row["service_name"]))

        chrome.searchable_table(
            gaps[cols],
            key=f"{group}_untagged_svc",
            search_col="service_name",
            money_cols=["untagged_cost", "gross_cost"],
            pct_cols=["tagged_pct"],
            rename=_UNTAGGED_RENAME,
            max_rows=_MAX_ROWS,
            on_row_click=_on_service_click,
        )

    top = str(gaps.iloc[0]["service_name"])
    _open_resources(top)


def _policy_tagging_remedies(provider_name: str) -> dict[str, str]:
    """entity_id → Policy tagging remedy for non-compliant rows (latest month).

    Empty when policy GOLD is missing or this provider has no tagging findings — the
    resource panel then uses the generic bill-tag remedy for every row.
    """
    if not gold_view_published("policy", "policy_record"):
        return {}
    cats = ", ".join(f"'{c}'" for c in sorted(_TAGGING_POLICY_CATEGORIES))
    rows = _df(
        "SELECT entity_id, policy_category FROM policy.policy_record "
        f"WHERE provider_name = '{_q(provider_name)}' "
        f"AND policy_category IN ({cats}) "
        "AND status = 'non_compliant' "
        "AND charge_month = ("
        "SELECT max(charge_month) FROM policy.policy_record "
        f"WHERE provider_name = '{_q(provider_name)}')"
    )
    if rows.empty:
        return {}
    out: dict[str, str] = {}
    for _, row in rows.iterrows():
        entity_id = str(row["entity_id"])
        remedy = _REMEDY_BY_CATEGORY.get(str(row["policy_category"]))
        if remedy and entity_id not in out:
            out[entity_id] = remedy
    return out


def untagged_resources(
    group: str,
    service_name: str,
    end: date,
    sm: date,
    *,
    scope_sql: str = "",
) -> None:
    """Ranked untagged resources for one service — the work queue under a service gap."""
    with chrome.panel():
        with ui.row().classes("items-center gap-1"):
            chrome.panel_title(f"Untagged resources — {service_name}")
            chrome.info_icon(_RESOURCE_INFO)

        if not gold_view_published(group, "spend_untagged_by_resource_month"):
            chrome.section_caption(
                "Untagged-by-resource isn't published yet — run `flashlight transform`."
            )
            return

        extra = f" AND {scope_sql}" if scope_sql else ""
        rows = _df(
            "SELECT resource_name, resource_id, resource_type, "
            "sub_account_id, region_id, sum(untagged_cost) AS untagged_cost "
            f'FROM "{group}".spend_untagged_by_resource_month '
            f"WHERE service_name = '{_q(service_name)}' "
            f"AND charge_month >= '{sm}' AND charge_month <= '{end}'{extra} "
            "GROUP BY resource_name, resource_id, resource_type, "
            "sub_account_id, region_id "
            "ORDER BY untagged_cost DESC"
        )
        if rows.empty:
            chrome.section_caption(
                f"No untagged resources for `{service_name}` in range "
                "(or this lake predates the resource view)."
            )
            return

        total = float(rows["untagged_cost"].sum())
        chrome.section_caption(
            f"{compact_money(total)} untagged across {len(rows):,} resource(s) — "
            "reconciles to this service's gap above."
        )
        chrome.caption_info(
            "How to fix: add cost-allocation tags on these resources.",
            _GENERIC_REMEDY,
        )

        remedies = _policy_tagging_remedies(provider_name_for_group(group))
        display = rows.assign(
            remedy=rows["resource_id"].map(
                lambda rid: remedies.get(str(rid), _GENERIC_REMEDY)
            ),
            resource_name=rows.apply(
                lambda r: (
                    "(no resource id on the bill)"
                    if str(r["resource_id"]) == "(none)"
                    and str(r["resource_name"]) in {"(none)", "(unattributed)"}
                    else r["resource_name"]
                ),
                axis=1,
            ),
        )
        # Drop workspace/region columns that are uniformly '(none)' — noise on providers
        # that never stamp them.
        cols = [c for c in _RESOURCE_COLS if c in display]
        for optional in ("sub_account_id", "region_id"):
            if optional in cols and set(display[optional].astype(str).unique()) == {"(none)"}:
                cols.remove(optional)

        chrome.searchable_table(
            display[cols],
            key=f"{group}_untagged_res",
            search_col="resource_name",
            money_cols=["untagged_cost"],
            rename=_RESOURCE_RENAME,
            max_rows=_MAX_ROWS,
        )


def _tag_key_rows(group: str, end: date, sm: date) -> pd.DataFrame:
    """Spend per folded tag key for the latest month in range, or empty.

    Latest month rather than the whole range on purpose: ``variant_count`` and
    ``tag_key_variants`` describe how a key is spelled *right now*, and a key renamed
    mid-range would otherwise read as two permanently-colliding spellings.
    """
    if not gold_view_published(group, "spend_by_tag_key_month"):
        return pd.DataFrame()
    return _df(
        f'SELECT * FROM "{group}".spend_by_tag_key_month WHERE charge_month = ('
        f'SELECT max(charge_month) FROM "{group}".spend_by_tag_key_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}') "
        "ORDER BY net_cost DESC"
    )


def _no_tagged_spend_panel(title: str) -> None:
    # Named rather than skipped: "this provider tags nothing" is a real finding, and
    # silently omitting the panel looks identical to the feature not existing.
    with chrome.panel():
        chrome.panel_title(title)
        chrome.section_caption(
            "No cost-allocation tags on this provider's charges."
        )


def tag_keys(
    group: str, end: date, sm: date, *, rows: pd.DataFrame | None = None
) -> None:
    """"Spend by tag key" panel.

    *rows* lets :func:`render` share one fetch with :func:`tag_values`. Used alone by
    ``redshift_focus``, which pairs it with its own SKU-scoped value drill.
    """
    if rows is None:
        rows = _tag_key_rows(group, end, sm)
    if rows.empty:
        _no_tagged_spend_panel("Spend by tag key — no tagged spend")
        return
    cols = [c for c in _TAG_COLS if c in rows]
    with chrome.panel():
        with ui.row().classes("items-center gap-1"):
            chrome.panel_title("Spend by tag key")
            chrome.info_icon(_TAG_KEY_INFO)
        chrome.searchable_table(
            rows[cols],
            key=f"{group}_tag_keys",
            search_col="tag_key_normalized",
            money_cols=["net_cost"],
            int_cols=["tag_value_count"],
            rename=_TAG_RENAME,
            max_rows=_MAX_ROWS,
        )


def _tag_value_rows(group: str, tag_key_normalized: str, end: date, sm: date) -> pd.DataFrame:
    """One folded key's values over the range.

    Filters ``spend_by_tag_month`` with the same case/separator fold
    ``spend_by_tag_key_month`` uses, rather than an exact match on its raw ``tag_key`` —
    otherwise picking "team" would miss dollars raw-tagged "Team", even though the
    ranking above already folded those two into one row.
    """
    return _df(
        "SELECT tag_value, sum(net_cost) AS net_cost FROM "
        f'"{group}".spend_by_tag_month '
        f"WHERE replace(lower(trim(tag_key)), '-', '_') = '{_q(tag_key_normalized)}' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY tag_value ORDER BY net_cost DESC LIMIT 20"
    )


def tag_values(
    group: str, end: date, sm: date, *, rows: pd.DataFrame | None = None
) -> None:
    """"Spend by tag value" panel — pick a folded key, rank its values.

    Key options come from the same folded ranking :func:`tag_keys` shows, so picking
    "team" also covers dollars raw-tagged "Team".
    """
    if rows is None:
        rows = _tag_key_rows(group, end, sm)
    if rows.empty:
        return

    options = rows["tag_key_normalized"].tolist()
    default = "team" if "team" in options else options[0]
    with chrome.panel():
        chrome.panel_title("Spend by tag value")
        body_container = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _values_body(sel: str) -> None:
            body_container.clear()
            with body_container:
                tags = _tag_value_rows(group, sel, end, sm)
                if tags.empty:
                    _info(f"No values for `{sel}` in range.")
                    return
                chrome.searchable_table(
                    tags,
                    key=f"{group}_tags",
                    search_col="tag_value",
                    money_cols=["net_cost"],
                    rename={"tag_value": sel, "net_cost": "Net cost"},
                )

        (
            ui.select(
                options=options, value=default, on_change=lambda e: _values_body.refresh(e.value)
            )
            .props("dense outlined")
            .classes("w-48")
            .style(f"color:{chrome.INK_PRIMARY}")
        )
        _values_body(default)
