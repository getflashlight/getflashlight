"""Attribution — what one provider's spend is tagged to.

The "Attribution" tab on every provider page (``provider_focus``, ``redshift_focus``).
Panels, top to bottom:

* **Untagged infrastructure** (:func:`untagged_infrastructure`) — ONE table, drilled
  through in place rather than stacked as separate service/resource panels. Three
  levels, breadcrumbed:

  1. *Service* (``<group>.spend_untagged_by_service_month``) — where tagging is missing.
  2. *Resource* (``<group>.spend_untagged_by_resource_month``) — what to open and tag,
     under the clicked service; dollars reconcile to that service's untagged total.
  3. *Drivers* (``efficiency.utilization_entity_month``, ``entity_type='sql_warehouse_user'``)
     — only reachable from a **shared, sub-metered** resource (see below); who is
     actually running the shared compute this resource billed for.

* **Spend by tag key** (:func:`tag_breakdown`) — what *is* tagged. The same
  drill-through shape as above, one level: click a key row to replace the ranking with
  that key's values (breadcrumbed back), instead of a ranking table sitting above a
  second panel with its own key-picker dropdown.

Billing granularity is NOT one fact ("tag the resource") — it is three, and the remedy
at level 2 differs by which is true for a row's ``service_name`` (:func:`_tier_for_service`):

* **dedicated** — ``resource_id`` on the bill already IS the billed unit (a Databricks
  job/notebook, a serving endpoint). The resource-level row is already the finest real
  grain; "tag it directly" is a complete answer.
* **shared, sub-metered** — a SQL warehouse (Databricks) or a Redshift cluster. The bill
  metres the *warehouse*, never a query (DBUs/slot-seconds aren't billed per-query — see
  ``efficiency/model.py``'s ``EntityType`` docstring and ``policy_rules.py``'s blocked
  ``query_tagging`` rule: no statement-level tag telemetry exists to build one). But a
  per-user *estimate* already exists — ``entity_type='sql_warehouse_user'`` allocates the
  warehouse's real ``billed_cost`` by each user's share of query duration that month,
  always ``candidate`` confidence, computed by both the Databricks and Redshift efficiency
  pulls. That's level 3 here: "tag the warehouse" alone hides who actually drove it, so a
  shared/sub-metered row drills one level further into that estimate instead of stopping
  at one generic sentence.
* **shared, no sub-grain** — an all-purpose/interactive cluster. Also billed as one shared
  unit, but nothing pulls a per-user split for it today (only SQL warehouses get
  ``sql_warehouse_user``). Named as shared rather than silently treated as dedicated, but
  there is no level 3 to drill into — inventing one would be exactly the fabricated
  per-query split the codebase already refuses to build.
* **unclassified** — every other ``service_name`` (storage, networking, AI products, …).
  Falls back to the pre-existing generic tag remedy rather than guessing a tier wrong in
  either direction.

Tier boundaries live in Python (:data:`_DEDICATED_SERVICES` etc.), duplicating —
deliberately, not by oversight — ``gold.compute_family`` in ``030_gold_metrics.sql``: that
macro is defined transform-time only and isn't registered on the dashboard's read-only
GOLD connection (:func:`flashlight.dashboard.data.gold_df` only registers published
Parquet). The two are intentionally not identical: ``compute_family`` folds Databricks
serverless notebooks into its coarse "interactive" bucket for a *cost rollup*; notebooks
bill at a real per-notebook grain (see ``databricks_efficiency.sql``'s ``notebook`` branch)
so they're **dedicated** here, not shared.

A resource row's remedy is the tier default UNLESS ``resource_id`` matches a non-compliant
Policy tagging finding (``cluster_tagging``/``warehouse_tagging``/``endpoint_tagging``),
which is always more specific and wins.

``spend_tag_coverage_month`` remains the provider-level denominator for agents. Policy
Compliance still owns entity-level tagging rules; this tab does not send users *only*
there — many bill gaps have no Policy row.

Per-tag-key spend is never totalled (a resource with two tags is counted under both —
the honest denominator is ``spend_tag_coverage_month.tagged_cost``). Methodology belongs
behind :func:`chrome.info_icon`, not a standing caption.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published, provider_name_for_group
from flashlight.dashboard.theme import compact_money
from flashlight.efficiency.policy_rules import POLICY_RULES
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES

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

_DRIVER_COLS = ["owner_user", "billed_cost", "duration_share_pct", "secondary_signals"]
_DRIVER_RENAME = {
    "owner_user": "User",
    "billed_cost": "Est. cost",
    "duration_share_pct": "Query-duration share",
    "secondary_signals": "Detail",
}

_MAX_ROWS = 40

# ── Billing-granularity tiers (see module docstring) ─────────────────────────────
_DEDICATED_SERVICES = frozenset({"JOBS", "DLT", "MODEL_SERVING", "NOTEBOOKS"})
_SHARED_SUBGRAIN_SERVICES = frozenset({"SQL"}) | REDSHIFT_SERVICE_NAMES
_SHARED_NO_SUBGRAIN_SERVICES = frozenset(
    {"ALL_PURPOSE", "INTERACTIVE", "SHARED_SERVERLESS_COMPUTE"}
)

_TIER_SCOPE_LABEL = {
    "dedicated": "Dedicated",
    "shared_subgrain": "Shared compute",
    "shared_no_subgrain": "Shared compute",
    "unclassified": "—",
}

_TIER_HEADLINE = {
    "dedicated": "already the finest billed grain.",
    "shared_subgrain": "billed as one meter; a per-user estimate exists below.",
    "shared_no_subgrain": "billed as one meter; no per-user split exists for it.",
    "unclassified": "grain not classified for this service.",
}

_TIER_REMEDY = {
    "dedicated": (
        "This is billed as its own resource — add cost-allocation tags on it directly "
        "(job/notebook/endpoint config) so its spend is attributed."
    ),
    "shared_subgrain": (
        "Shared compute — it isn't billed per query, so tagging it attributes ALL of it "
        "as one bucket. Click this row for the estimated per-user drivers, then isolate "
        "the heaviest user (a dedicated/right-sized warehouse) or tag work at submission "
        "time."
    ),
    "shared_no_subgrain": (
        "Shared cluster — no per-user billing split exists for it today, so tagging it "
        "still attributes all of it as one bucket. Tag the cluster/cluster policy, or "
        "move scheduled work onto Jobs compute, which bills — and can be tagged — per job."
    ),
    "unclassified": (
        "Add cost-allocation tags (e.g. team, project, environment) on this resource in "
        "the cloud console or as code so its spend can be attributed."
    ),
}

_TAGGING_POLICY_CATEGORIES = frozenset(
    {"cluster_tagging", "warehouse_tagging", "endpoint_tagging"}
)
_REMEDY_BY_CATEGORY = {
    r.category: r.remedy for r in POLICY_RULES if r.category in _TAGGING_POLICY_CATEGORIES
}

_TAG_KEY_INFO = (
    "Tagged charges only; don't sum Spend (multi-tagged resources count twice). "
    "Case/separator variants fold into one row — Spelled as lists the raw forms."
)

_UNTAGGED_INFO = (
    "Charges with no cost-allocation tag. Credits excluded. Click a service to see the "
    "resources behind it, and a shared warehouse's row for its estimated drivers."
)


def _tier_for_service(service_name: str) -> str:
    """Which billing-granularity tier a ``service_name`` falls into. See module docstring."""
    if service_name in _DEDICATED_SERVICES:
        return "dedicated"
    if service_name in _SHARED_SUBGRAIN_SERVICES:
        return "shared_subgrain"
    if service_name in _SHARED_NO_SUBGRAIN_SERVICES:
        return "shared_no_subgrain"
    return "unclassified"


def _q(value: str) -> str:
    """Escape a string for inlining as a single-quoted SQL literal."""
    return value.replace("'", "''")


def _df(sql: str) -> pd.DataFrame:
    """Query an attribution view, returning empty on any issue (view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render the empty state
        return pd.DataFrame()


def render(group: str, end: date, sm: date) -> None:
    """The whole "Attribution" tab body for one provider.

    Draws its own panels — callers must NOT wrap this in ``chrome.panel()`` (the same
    convention ``provider_focus``'s ``extra_tabs`` follow).
    """
    untagged_infrastructure(group, end, sm)
    tag_breakdown(group, end, sm)


@dataclass(frozen=True)
class _Drill:
    """One position in the service → resource → driver breadcrumb.

    A value object, not mutated in place — every navigation constructs a fresh
    ``_Drill`` and hands it to the owning ``@ui.refreshable``'s ``.refresh()``.
    That keeps the state local to one panel's closure; nothing here is module-level, so
    two provider pages (or two browser tabs) drilling at once can't collide. Same shape
    as :func:`tag_breakdown`'s ``_TagDrill``, one level deeper.
    """

    level: str = "service"
    service: str | None = None
    resource_id: str | None = None
    resource_name: str | None = None


def untagged_infrastructure(
    group: str, end: date, sm: date, *, scope_sql: str = ""
) -> None:
    """ONE drill-through panel: service → resource → (shared warehouses only) drivers.

    *scope_sql* is an optional extra predicate (no leading AND/WHERE), used by
    ``redshift_focus`` to narrow to Redshift service names. Empty = whole provider.
    """
    provider = provider_name_for_group(group)

    with chrome.panel():
        title = ui.label("Untagged infrastructure").classes(
            "text-sm font-medium mb-2"
        ).style(f"color:{chrome.INK_SECONDARY}")
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _Drill) -> None:
            body.clear()
            title.text = (
                "Untagged infrastructure"
                if state.level == "service"
                else f"Untagged infrastructure — {state.service}"
                if state.level == "resource"
                else f"Untagged infrastructure — {state.service} — {state.resource_name}"
            )
            with body:
                if state.level == "service":
                    _render_service_level(
                        group, end, sm, scope_sql=scope_sql, refresh=_body.refresh
                    )
                elif state.level == "resource":
                    assert state.service is not None
                    _render_resource_level(
                        group,
                        provider,
                        state.service,
                        end,
                        sm,
                        scope_sql=scope_sql,
                        refresh=_body.refresh,
                    )
                else:
                    assert state.service is not None and state.resource_id is not None
                    _render_driver_level(provider, end, sm, state=state, refresh=_body.refresh)

        _body(_Drill())


def _breadcrumb[T](*steps: tuple[str, T | None], refresh: Callable[[T], object]) -> None:
    """``"← A / B"`` — every step but the last is a link back to that state.

    Generic over the drill state type so :func:`untagged_infrastructure`'s ``_Drill``
    and :func:`tag_breakdown`'s ``_TagDrill`` share one implementation.
    """
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


def _render_service_level(
    group: str, end: date, sm: date, *, scope_sql: str, refresh: Callable[[_Drill], object]
) -> None:
    """Level 1: services with untagged charges — KPI row + ranked table."""
    if not gold_view_published(group, "spend_untagged_by_service_month"):
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
    if rows.empty:
        chrome.section_caption("No charges in range to measure tag coverage against.")
        return

    # Strict gap filter — exclude floating $0 / fully-tagged noise.
    gaps = rows.loc[rows["untagged_cost"] > 0].copy()
    gross = float(rows["gross_cost"].sum())
    untagged = float(gaps["untagged_cost"].sum()) if not gaps.empty else 0.0
    n_gap = len(gaps)
    share = f"{100 * untagged / gross:.0f}% of charges" if gross > 0 else "—"

    with ui.row().classes("items-center gap-1"):
        chrome.info_icon(_UNTAGGED_INFO)
    chrome.stat_row(
        [
            ("Untagged", compact_money(untagged), share, "unattributed"),
            ("Services with gaps", f"{n_gap:,}", f"of {len(rows):,} with charges"),
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
    chrome.section_caption("Click a service to see the resources behind it.")

    def _on_click(row: dict[str, object]) -> None:
        refresh(_Drill(level="resource", service=str(row["service_name"])))

    chrome.searchable_table(
        gaps[cols],
        key=f"{group}_untagged_svc",
        search_col="service_name",
        money_cols=["untagged_cost", "gross_cost"],
        pct_cols=["tagged_pct"],
        rename=_UNTAGGED_RENAME,
        max_rows=_MAX_ROWS,
        on_row_click=_on_click,
    )


def _policy_tagging_remedies(provider_name: str) -> dict[str, str]:
    """entity_id → Policy tagging remedy for non-compliant rows (latest month).

    Empty when policy GOLD is missing or this provider has no tagging findings — the
    resource panel then uses the tier's default remedy for every row.
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


def _render_resource_level(
    group: str,
    provider: str,
    service_name: str,
    end: date,
    sm: date,
    *,
    scope_sql: str,
    refresh: Callable[[_Drill], object],
) -> None:
    """Level 2: ranked untagged resources for one service, with a tier-specific remedy."""
    _breadcrumb(
        ("← All services", _Drill()),
        (service_name, None),
        refresh=refresh,
    )

    tier = _tier_for_service(service_name)
    chrome.caption_info(
        f"{_TIER_SCOPE_LABEL[tier]} — {_TIER_HEADLINE[tier]}", _TIER_REMEDY[tier]
    )

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
        "reconciles to this service's gap."
    )
    if tier == "shared_subgrain":
        chrome.section_caption("Click a row for its estimated per-user drivers.")

    remedies = _policy_tagging_remedies(provider)
    tier_remedy = _TIER_REMEDY[tier]
    display = rows.assign(
        remedy=rows["resource_id"].map(lambda rid: remedies.get(str(rid), tier_remedy)),
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
    cols = [c for c in _RESOURCE_COLS if c in display]
    for optional in ("sub_account_id", "region_id"):
        if optional in cols and set(display[optional].astype(str).unique()) == {"(none)"}:
            cols.remove(optional)

    on_click: Callable[[dict[str, object]], None] | None = None
    if tier == "shared_subgrain":

        def on_click(row: dict[str, object]) -> None:  # noqa: F811
            rid = str(row["resource_id"])
            if rid == "(none)":
                return  # no identity to look drivers up by
            refresh(
                _Drill(
                    level="driver",
                    service=service_name,
                    resource_id=rid,
                    resource_name=str(row["resource_name"]),
                )
            )

    chrome.searchable_table(
        display[cols],
        key=f"{group}_untagged_res",
        search_col="resource_name",
        money_cols=["untagged_cost"],
        rename=_RESOURCE_RENAME,
        max_rows=_MAX_ROWS,
        on_row_click=on_click,
    )


def _render_driver_level(
    provider: str, end: date, sm: date, *, state: _Drill, refresh: Callable[[_Drill], object]
) -> None:
    """Level 3: estimated per-user drivers of one shared, sub-metered resource.

    Only reachable from a ``shared_subgrain`` resource row — see the tier table in the
    module docstring for why every other tier has no level 3 to show.
    """
    assert state.service is not None
    assert state.resource_id is not None
    _breadcrumb(
        ("← All services", _Drill()),
        (state.service, _Drill(level="resource", service=state.service)),
        (state.resource_name or state.resource_id, None),
        refresh=refresh,
    )
    chrome.caption_info(
        "Estimated by query-duration share, latest month with telemetry in range — "
        "not an exact per-query split.",
        "DBUs/slot-seconds aren't billed per query, so this allocates the resource's real "
        "billed cost by each user's share of measured query duration that month. Always "
        "an estimate under concurrency (candidate confidence), never claimed exact.",
    )

    resource_id = _q(state.resource_id)
    rows = _df(
        "SELECT owner_user, billed_cost, primary_signal_value AS duration_share_pct, "
        "secondary_signals FROM efficiency.utilization_entity_month "
        f"WHERE provider_name = '{_q(provider)}' AND entity_type = 'sql_warehouse_user' "
        f"AND entity_id LIKE '{resource_id}:%' "
        "AND charge_month = (SELECT max(charge_month) FROM efficiency.utilization_entity_month "
        f"WHERE provider_name = '{_q(provider)}' AND entity_type = 'sql_warehouse_user' "
        f"AND entity_id LIKE '{resource_id}:%' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}') "
        "ORDER BY billed_cost DESC"
    )
    if rows.empty:
        chrome.section_caption(
            "No per-user telemetry for this resource in range — the efficiency pull may "
            "not have run, or this warehouse had no measured query activity."
        )
        return

    cols = [c for c in _DRIVER_COLS if c in rows]
    chrome.searchable_table(
        rows[cols],
        key=f"untagged_drivers_{resource_id}",
        search_col="owner_user",
        money_cols=["billed_cost"],
        pct_cols=["duration_share_pct"],
        rename=_DRIVER_RENAME,
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


def tag_keys(group: str, end: date, sm: date) -> None:
    """"Spend by tag key" panel, standalone and non-drilling.

    Used only by ``redshift_focus``, which pairs it with its own SKU-scoped value
    drill (:func:`_tags_section` there) rather than :func:`tag_breakdown`'s.
    """
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


@dataclass(frozen=True)
class _TagDrill:
    """One position in the tag key → value breadcrumb — same shape as ``_Drill``, one
    level instead of three."""

    level: str = "key"
    tag_key: str | None = None


def tag_breakdown(group: str, end: date, sm: date) -> None:
    """ONE drill-through panel: tag key ranking → values for the clicked key.

    Replaces the old shape (a ranking table, then a separate "Spend by tag value"
    panel with its own key-picker dropdown) with the same click-a-row-to-drill pattern
    :func:`untagged_infrastructure` uses.
    """
    rows = _tag_key_rows(group, end, sm)
    if rows.empty:
        _no_tagged_spend_panel("Spend by tag key — no tagged spend")
        return

    with chrome.panel():
        title = ui.label("Spend by tag key").classes(
            "text-sm font-medium mb-2"
        ).style(f"color:{chrome.INK_SECONDARY}")
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _TagDrill) -> None:
            body.clear()
            title.text = (
                "Spend by tag key"
                if state.level == "key"
                else f"Spend by tag key — {state.tag_key}"
            )
            with body:
                if state.level == "key":
                    _render_tag_key_level(group, rows=rows, refresh=_body.refresh)
                else:
                    assert state.tag_key is not None
                    _render_tag_value_level(group, state.tag_key, end, sm, refresh=_body.refresh)

        _body(_TagDrill())


def _render_tag_key_level(
    group: str, *, rows: pd.DataFrame, refresh: Callable[[_TagDrill], object]
) -> None:
    """Level 1: the folded key ranking — content unchanged from the old standalone
    panel, just with a row click instead of feeding a separate dropdown."""
    with ui.row().classes("items-center gap-1"):
        chrome.info_icon(_TAG_KEY_INFO)
    chrome.section_caption("Click a key to see its values.")
    cols = [c for c in _TAG_COLS if c in rows]

    def _on_click(row: dict[str, object]) -> None:
        refresh(_TagDrill(level="value", tag_key=str(row["tag_key_normalized"])))

    chrome.searchable_table(
        rows[cols],
        key=f"{group}_tag_keys",
        search_col="tag_key_normalized",
        money_cols=["net_cost"],
        int_cols=["tag_value_count"],
        rename=_TAG_RENAME,
        max_rows=_MAX_ROWS,
        on_row_click=_on_click,
    )


def _render_tag_value_level(
    group: str, tag_key: str, end: date, sm: date, *, refresh: Callable[[_TagDrill], object]
) -> None:
    """Level 2: one folded key's values, breadcrumbed back to the ranking."""
    _breadcrumb(("← All tag keys", _TagDrill()), (tag_key, None), refresh=refresh)

    tags = _tag_value_rows(group, tag_key, end, sm)
    if tags.empty:
        chrome.section_caption(f"No values for `{tag_key}` in range.")
        return
    chrome.searchable_table(
        tags,
        key=f"{group}_tag_values_{tag_key}",
        search_col="tag_value",
        money_cols=["net_cost"],
        rename={"tag_value": tag_key, "net_cost": "Net cost"},
        max_rows=_MAX_ROWS,
    )
