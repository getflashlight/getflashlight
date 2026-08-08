"""Attribution — who and what is responsible for one provider's spend.

The "Attribution" tab on every provider page (``provider_focus``, ``redshift_focus``).
Panels, top to bottom:

* **Cost attribution** (:func:`cost_attribution`) — ONE table, drilled
  through in place rather than stacked as separate service/resource panels. Three
  levels, breadcrumbed:

  1. *Service* (``<group>.spend_by_service_month``) — where the money is going.
  2. *Resource* (``<group>.resource_month``) — the billed infrastructure under that
     service; charges reconcile exactly to the selected service.
  3. *Drivers* (``efficiency.utilization_entity_month``, ``entity_type='sql_warehouse_user'``)
     — only reachable from a **shared, sub-metered** resource (see below); who is
     actually running the shared compute this resource billed for.

* **Tag-based attribution** (:func:`tag_breakdown`) — a second, independent
  drill-down over every tag key found on the customer's charges. Selecting a key
  shows its values (for example team, project, environment, or customer-defined
  tags). A charge carrying multiple tags appears under each of those keys, so tag-key
  totals are intentionally not summed together.

Billing granularity is NOT one fact ("tag the resource") — it is three
(:func:`_tier_for_service`), and only one of them can drill to level 3:

* **dedicated** — ``resource_id`` on the bill already IS the billed unit (a Databricks
  job/notebook, a serving endpoint). Already the finest real grain; no level 3.
* **shared, sub-metered** — a SQL warehouse (Databricks) or a Redshift cluster. The bill
  metres the *warehouse*, never a query (DBUs/slot-seconds aren't billed per-query — see
  ``efficiency/model.py``'s ``EntityType`` docstring and ``policy_rules.py``'s blocked
  ``query_tagging`` rule: no statement-level tag telemetry exists to build one). But a
  per-user *estimate* already exists — ``entity_type='sql_warehouse_user'`` allocates the
  warehouse's real ``billed_cost`` by each user's share of query duration that month,
  always ``candidate`` confidence, computed by both the Databricks and Redshift efficiency
  pulls. That's level 3: a row in this tier drills one level further into that estimate.
* **shared, no sub-grain** — an all-purpose/interactive cluster. Also billed as one shared
  unit, but nothing pulls a per-user split for it today (only SQL warehouses get
  ``sql_warehouse_user``), so there is no level 3 to drill into — inventing one would be
  exactly the fabricated per-query split the codebase already refuses to build.
* **unclassified** — every other ``service_name`` (storage, networking, AI products, …).

Tier boundaries live in Python (:data:`_DEDICATED_SERVICES` etc.), duplicating —
deliberately, not by oversight — ``gold.compute_family`` in ``030_gold_metrics.sql``: that
macro is defined transform-time only and isn't registered on the dashboard's read-only
GOLD connection (:func:`flashlight.dashboard.data.gold_df` only registers published
Parquet). The two are intentionally not identical: ``compute_family`` folds Databricks
serverless notebooks into its coarse "interactive" bucket for a *cost rollup*; notebooks
bill at a real per-notebook grain (see ``databricks_efficiency.sql``'s ``notebook`` branch)
so they're **dedicated** here, not shared.

The hierarchy always uses charge-side cost (credits excluded): service → resource →
user allocations. A user allocation is calculated from the same resource/month charge
and that user's measured query-duration share. Any resource-month without telemetry is
shown as unallocated, rather than silently disappearing, so every drill-down total
continues to match its parent.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published, provider_name_for_group
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES

_TAG_COLS = ["tag_key_normalized", "tag_key_variants", "net_cost", "tag_value_count"]
_TAG_RENAME = {
    "tag_key_normalized": "Tag key",
    "tag_key_variants": "Spelled as",
    "net_cost": "Spend",
    "tag_value_count": "Values",
}

_SERVICE_COLS = ["service_name", "gross_cost", "share_pct"]
_SERVICE_RENAME = {
    "service_name": "Service",
    "gross_cost": "Cost",
    "share_pct": "Share of total",
}

_RESOURCE_COLS = [
    "resource_name",
    "resource_id",
    "resource_type",
    "sub_account_id",
    "region_id",
    "gross_cost",
]
_RESOURCE_RENAME = {
    "resource_name": "Resource",
    "resource_id": "Id",
    "resource_type": "Type",
    "sub_account_id": "Workspace",
    "region_id": "Region",
    "gross_cost": "Cost",
}

_DRIVER_COLS = ["owner_user", "allocated_cost", "duration_share_pct", "secondary_signals"]
_DRIVER_RENAME = {
    "owner_user": "User",
    "allocated_cost": "Cost",
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

_TAG_KEY_INFO = (
    "Tagged charges only; don't sum Spend (multi-tagged resources count twice). "
    "Case/separator variants fold into one row — Spelled as lists the raw forms."
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
    cost_attribution(group, end, sm)
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
    resource_type: str | None = None
    sub_account_id: str | None = None
    region_id: str | None = None


def cost_attribution(
    group: str, end: date, sm: date, *, scope_sql: str = ""
) -> None:
    """ONE drill-through panel: service → resource → (shared warehouses only) drivers.

    *scope_sql* is an optional extra predicate (no leading AND/WHERE), used by
    ``redshift_focus`` to narrow to Redshift service names. Empty = whole provider.
    """
    provider = provider_name_for_group(group)

    with chrome.panel():
        title = ui.label("Cost attribution").classes(
            "text-sm font-medium mb-2"
        ).style(f"color:{chrome.INK_SECONDARY}")
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _Drill) -> None:
            body.clear()
            title.text = (
                "Cost attribution"
                if state.level == "service"
                else f"Cost attribution — {state.service}"
                if state.level == "resource"
                else f"Cost attribution — {state.service} — {state.resource_name}"
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
                        state.service,
                        end,
                        sm,
                        scope_sql=scope_sql,
                        refresh=_body.refresh,
                    )
                else:
                    assert state.service is not None and state.resource_id is not None
                    _render_driver_level(
                        group, provider, end, sm, state=state, refresh=_body.refresh
                    )

        _body(_Drill())


def _breadcrumb[T](*steps: tuple[str, T | None], refresh: Callable[[T], object]) -> None:
    """``"← A / B"`` — every step but the last is a link back to that state.

    Generic over the drill state type so :func:`cost_attribution`'s ``_Drill``
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
    """Level 1: every charged service, ranked by the same charge basis as resources."""
    if not gold_view_published(group, "spend_by_service_month"):
        chrome.section_caption(
            "Spend by service isn't published yet — run `flashlight transform`."
        )
        return

    extra = f" AND {scope_sql}" if scope_sql else ""
    rows = _df(
        "SELECT service_name, sum(gross_cost) AS gross_cost "
        f'FROM "{group}".spend_by_service_month '
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'{extra} "
        "GROUP BY service_name"
    )
    if rows.empty:
        chrome.section_caption("No charges in range.")
        return

    gross = float(rows["gross_cost"].sum())

    rows = rows.loc[rows["gross_cost"] != 0].copy()
    # ``gross`` is a scalar.  Pandas ``Series.where`` expects a same-shaped
    # condition, so branch here instead of handing it a scalar bool (which broke
    # the tab as soon as there was more than one service).
    rows = rows.assign(
        share_pct=100 * rows["gross_cost"] / gross if gross != 0 else None
    ).sort_values("gross_cost", ascending=False)
    cols = [c for c in _SERVICE_COLS if c in rows]
    def _on_click(row: dict[str, object]) -> None:
        refresh(_Drill(level="resource", service=str(row["service_name"])))

    chrome.searchable_table(
        rows[cols],
        key=f"{group}_attribution_svc",
        search_col="service_name",
        money_cols=["gross_cost"],
        pct_cols=["share_pct"],
        rename=_SERVICE_RENAME,
        max_rows=_MAX_ROWS,
        on_row_click=_on_click,
    )


def _render_resource_level(
    group: str,
    service_name: str,
    end: date,
    sm: date,
    *,
    scope_sql: str,
    refresh: Callable[[_Drill], object],
) -> None:
    """Level 2: billed resources for one service.

    Used to carry a tier caption, a "$X across N resources" recap, a "click a row for
    drivers" hint and a Policy-override count above the table — four lines of prose
    for what the table and its breadcrumb already show. Removed; the tier still gates
    whether a row drills to level 3 (see *on_click* below), it just isn't narrated.
    """
    _breadcrumb(
        ("← All services", _Drill()),
        (service_name, None),
        refresh=refresh,
    )

    tier = _tier_for_service(service_name)

    if not gold_view_published(group, "resource_month"):
        chrome.section_caption(
            "Spend by resource isn't published yet — run `flashlight transform`."
        )
        return

    extra = f" AND {scope_sql}" if scope_sql else ""
    rows = _df(
        "SELECT resource_name, resource_id, resource_type, "
        "sub_account_id, region_id, sum(gross_cost) AS gross_cost "
        f'FROM "{group}".resource_month '
        f"WHERE service_name = '{_q(service_name)}' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}'{extra} "
        "GROUP BY resource_name, resource_id, resource_type, "
        "sub_account_id, region_id "
        "ORDER BY gross_cost DESC"
    )
    if rows.empty:
        chrome.section_caption(
            f"No resources for `{service_name}` in range "
            "(run `flashlight transform` after upgrading if this lake predates "
            "gross resource cost)."
        )
        return

    display = rows.assign(
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
                    resource_type=str(row["resource_type"]),
                    sub_account_id=str(row.get("sub_account_id", "(none)")),
                    region_id=str(row.get("region_id", "(none)")),
                )
            )

    chrome.searchable_table(
        display[cols],
        key=f"{group}_attribution_res",
        search_col="resource_name",
        money_cols=["gross_cost"],
        rename=_RESOURCE_RENAME,
        max_rows=_MAX_ROWS,
        on_row_click=on_click,
    )


def _render_driver_level(
    group: str,
    provider: str,
    end: date,
    sm: date,
    *,
    state: _Drill,
    refresh: Callable[[_Drill], object],
) -> None:
    """Level 3: estimated per-user drivers of one shared, sub-metered resource.

    Only reachable from a ``shared_subgrain`` resource row — see the tier table in the
    module docstring for why every other tier has no level 3 to show.
    """
    assert state.service is not None
    assert state.resource_id is not None
    assert state.resource_name is not None
    assert state.resource_type is not None
    assert state.sub_account_id is not None
    assert state.region_id is not None
    _breadcrumb(
        ("← All services", _Drill()),
        (state.service, _Drill(level="resource", service=state.service)),
        (state.resource_name or state.resource_id, None),
        refresh=refresh,
    )
    chrome.caption_info(
        "Estimated from query-duration share; unmeasured months remain unallocated.",
        "For every month in the selected range, each user's measured query-duration share "
        "is multiplied by this warehouse's actual charge. DBUs/slot-seconds are not billed "
        "per query, so this is an estimate under concurrency. The unallocated row is the "
        "remainder from months with no query telemetry, keeping this table equal to the "
        "warehouse total above.",
    )

    resource_id = _q(state.resource_id)
    # Allocate from resource_month rather than telemetry.billed_cost.  The former is
    # the exact charge basis used by levels 1 and 2; telemetry's own cost is useful
    # operational evidence but can cover a different pull window.  A residual row
    # makes missing telemetry explicit and preserves parent/child reconciliation.
    rows = _df(
        "WITH resource_cost AS ("
        " SELECT charge_month, sum(gross_cost) AS resource_cost "
        f' FROM "{group}".resource_month '
        f" WHERE service_name = '{_q(state.service)}' AND resource_id = '{resource_id}' "
        f" AND resource_name = '{_q(state.resource_name)}' "
        f" AND resource_type = '{_q(state.resource_type)}' "
        f" AND sub_account_id = '{_q(state.sub_account_id)}' "
        f" AND region_id = '{_q(state.region_id)}' "
        f" AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        " GROUP BY charge_month"
        "), raw_user_share AS ("
        " SELECT charge_month, owner_user, max(primary_signal_value) / 100.0 AS duration_share, "
        " max(secondary_signals) AS secondary_signals "
        " FROM efficiency.utilization_entity_month "
        f" WHERE provider_name = '{_q(provider)}' AND entity_type = 'sql_warehouse_user' "
        f" AND entity_id LIKE '{resource_id}:%' "
        f" AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        " GROUP BY charge_month, owner_user"
        "), user_share AS ("
        " SELECT *, sum(duration_share) OVER (PARTITION BY charge_month) AS total_duration_share "
        " FROM raw_user_share"
        "), allocations AS ("
        " SELECT u.owner_user, r.resource_cost * u.duration_share / "
        " nullif(u.total_duration_share, 0) AS allocated_cost, "
        " u.duration_share, u.secondary_signals FROM resource_cost r "
        " JOIN user_share u USING (charge_month)"
        "), user_totals AS ("
        " SELECT owner_user, sum(allocated_cost) AS allocated_cost, "
        " 100.0 * sum(allocated_cost) / "
        " nullif((SELECT sum(resource_cost) FROM resource_cost), 0) AS duration_share_pct, "
        " max(secondary_signals) AS secondary_signals FROM allocations GROUP BY owner_user"
        "), remainder AS ("
        " SELECT 'Unallocated (no query telemetry)' AS owner_user, "
        " greatest(0, coalesce((SELECT sum(resource_cost) FROM resource_cost), 0) - "
        " coalesce((SELECT sum(allocated_cost) FROM allocations), 0)) AS allocated_cost, "
        " NULL::DOUBLE AS duration_share_pct, 'No measured query activity' AS secondary_signals"
        ") SELECT * FROM user_totals UNION ALL SELECT * FROM remainder "
        " ORDER BY allocated_cost DESC"
    )
    if rows.empty:
        chrome.section_caption(
            "No billable resource cost for this warehouse in range."
        )
        return

    cols = [c for c in _DRIVER_COLS if c in rows]
    chrome.searchable_table(
        rows[cols],
        key=f"attribution_drivers_{resource_id}",
        search_col="owner_user",
        money_cols=["allocated_cost"],
        pct_cols=["duration_share_pct"],
        rename=_DRIVER_RENAME,
        max_rows=_MAX_ROWS,
    )


def _tag_key_rows(group: str, end: date, sm: date) -> pd.DataFrame:
    """Charge-side spend per folded tag key across the selected date range.

    The tag panel follows the same global date filter as cost attribution. Variants
    deliberately merge all spellings observed in that range; a separate spelling row
    would fragment a customer's own team/project dimension.
    """
    if not gold_view_published(group, "spend_by_tag_key_month"):
        return pd.DataFrame()
    return _df(
        "WITH key_cost AS ("
        " SELECT tag_key_normalized, string_agg(DISTINCT tag_key_variants, ' · ') "
        " AS tag_key_variants, sum(net_cost) AS net_cost "
        f' FROM "{group}".spend_by_tag_key_month '
        f" WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        " GROUP BY tag_key_normalized"
        "), value_count AS ("
        " SELECT replace(lower(trim(tag_key)), '-', '_') AS tag_key_normalized, "
        " count(DISTINCT tag_value) AS tag_value_count "
        f' FROM "{group}".spend_by_tag_month '
        f" WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        " GROUP BY replace(lower(trim(tag_key)), '-', '_')"
        ") SELECT k.*, v.tag_value_count FROM key_cost k "
        " JOIN value_count v USING (tag_key_normalized) ORDER BY net_cost DESC"
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
        _no_tagged_spend_panel("Tag-based attribution — no tagged spend")
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
        "SELECT tag_value, sum(gross_cost) AS net_cost FROM "
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
    :func:`cost_attribution` uses.
    """
    rows = _tag_key_rows(group, end, sm)
    if rows.empty:
        _no_tagged_spend_panel("Spend by tag key — no tagged spend")
        return

    with chrome.panel():
        title = ui.label("Tag-based attribution").classes(
            "text-sm font-medium mb-2"
        ).style(f"color:{chrome.INK_SECONDARY}")
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _TagDrill) -> None:
            body.clear()
            title.text = (
                "Tag-based attribution"
                if state.level == "key"
                else f"Tag-based attribution — {state.tag_key}"
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
