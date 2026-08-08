"""Redshift-scoped view — the ``/aws`` page.

This is a **configuration of** :func:`provider_focus.render`, not a second
implementation of it. It used to be the latter: a full fork of the page shell, KPI row,
summary line and Trend/Breakdown panels, which meant the four identically-labelled tabs
held different panels on ``/aws`` than on every other provider page, and a panel added
to one silently didn't exist on the other. What forced the fork was that this page needs
a *sub-provider scope* — the ``aws`` group narrowed to Redshift's own FOCUS
``service_name`` values (:data:`REDSHIFT_SERVICE_NAMES`) — and the views it needed
carried no ``service_name`` to narrow by. Adding that dimension to
``spend_trend_daily`` (plus ``list_cost``/``savings`` to ``spend_by_service_month``)
removed the reason, so what's left here is only what is genuinely Redshift-shaped.

Not its own GOLD provider group — Redshift's cost already flows into ``aws.*`` via the
``aws_focus`` connector (AWS Data Exports FOCUS carries Redshift's own SKUs); its own
connector only supplies efficiency/waste telemetry. So this page:

- **Trend & changes** / **Breakdown**: the shared panels, narrowed by
  :func:`scope`. Three panels inside Breakdown are account-wide on purpose, not
  service-scoped — ``_credits`` (a credit is applied to the account, often with no
  ServiceName at all) and ``_commitment`` (an RI/Savings Plan belongs to the account,
  not one service); each says so in its own caption, and they're declared in
  :data:`_ACCOUNT_WIDE` so the scope leaves them alone. Breakdown is also led by this
  page's own :func:`_spend_partition`.
- **Attribution**: :func:`_attribution_section`, a Redshift-scoped cost hierarchy
  (service → cluster → user allocation) narrowed by ``service_name``.
- **Efficiency & Waste**: faceted per cluster (:func:`_waste_section`) — one section per
  billed Redshift cluster. Clusters with telemetry show their findings; clusters without
  it show an explicit, cluster-specific instrumentation gap. ``entity_id`` for
  ``entity_type='sql_warehouse'`` under ``provider_name='AWS'`` is the cluster
  identifier itself, and every other Redshift entity_type is ``<cluster_id>:...``
  prefixed, see ``ingest/connectors/redshift.py``). Clusters that bill on this
  account (visible in the cost section's "Spend by cluster") but have no
  ``redshift`` connector entry get their own explicit setup state, never a
  silently omitted or account-level answer. Each cluster's findings come from
  ``efficiency.waste_record`` filtered
  to ``provider_name = 'AWS'`` plus either a ``redshift_``-prefixed category
  (Redshift-only rules) or ``entity_type IN ('sql_warehouse', 'sql_warehouse_user')``
  — the two categories shared with Databricks (``idle``,
  ``sql_warehouse_user_concentration``) key off those entity types, and under
  ``provider_name = 'AWS'`` only the Redshift connector emits them (S3's own signal
  uses ``entity_type = 'storage'``), so the combination stays Redshift-only despite
  ``entity_type`` names being shared in *name* with Databricks' own entity type
  (just under a different ``provider_name``). ``efficiency_waste.coverage_summary``
  also cross-references ``efficiency.efficiency_entity_month`` (every rule this connector
  evaluates, not just the ones that fired) so a rule that found nothing reads differently
  from one whose telemetry never arrived this window. It is compact diagnostic text; the
  single table per cluster is the same drill-down action queue used by every provider.

  This tab is why the page does NOT also carry ``efficiency_waste.render()`` the way every
  other provider page does: ``_waste_section`` is scoped per *cluster*, which is finer than
  per provider, so a provider-scoped tab beside it would render the union of these sections
  — the same ``waste_record`` rows twice. What it does borrow is
  ``efficiency_waste.coverage_caption``, passed the same per-cluster ``scope`` predicate, so
  the "what didn't we measure?" statement is cluster-scoped like everything around it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published, provider_label
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.summary import action_group_rows
from flashlight.dashboard.theme import compact_money
from flashlight.dashboard.views import attribution, driver_health, efficiency_waste, provider_focus
from flashlight.dashboard.views.provider_focus import Scope
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES

_GROUP = "aws"
_PROVIDER = "AWS"
_SERVICE_IN = ", ".join(f"'{s}'" for s in sorted(REDSHIFT_SERVICE_NAMES))
_SKU_SCOPE = (
    f"sku_id IN (SELECT DISTINCT sku_id FROM {_GROUP}.resource_month "
    f"WHERE service_name IN ({_SERVICE_IN}))"
)

# Views this page reads WIDER than its own service scope, on purpose. Declared rather
# than inferred because the first three *do* carry service_name — `credits_month`
# especially — so a "the column exists, so filter by it" rule would silently narrow
# them. AWS applies a credit at account level and often tags it to no service, so
# filtering would hide part of the discount and corrupt _spend_partition's
# account-level bucket, which nets credits against unused commitment. The last two
# have no service dimension at all and are deliberately not being given one.
_ACCOUNT_WIDE = frozenset(
    {
        "credits_month",  # applied to the account, frequently with no ServiceName
        "commitment_summary_month",  # an RI / Savings Plan isn't tied to one service
        "spend_by_tag_key_month",
        "spend_by_tag_month",
    }
)


def scope() -> Scope:
    """The ``aws`` group narrowed to Redshift's own FOCUS service names.

    This is the whole reason ``/aws`` needed its own module: the ``aws_focus`` connector
    ingests a Redshift-scoped slice of the bill (``include_services``, Redshift by
    default), so the group is AWS but the page is Redshift.
    """
    return Scope(
        group=_GROUP,
        dimension="service_name",
        values=tuple(sorted(REDSHIFT_SERVICE_NAMES)),
        account_wide=_ACCOUNT_WIDE,
        # The provider-wide GOLD forecast carries no service_name. Fit the same
        # conservative hold/run-rate from Redshift-only daily actuals instead.
        scoped_forecast=True,
    )


def _sql_str(value: str) -> str:
    return value.replace("'", "''")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)


def render() -> None:
    """The ``/aws`` page — the shared provider page, narrowed to Redshift.

    Everything above the tabs (title, date range, KPI row, summary line) and the whole
    of Trend & changes now come from :func:`provider_focus.render`, so ``/aws`` gets the
    daily trend, the clickable month drill and the list/savings/realized-discount KPIs
    it never had, and can't drift from the other provider pages again.
    """
    provider_focus.render(
        _GROUP,
        # The group's display label, not a literal — one place decides whether this page
        # is called "AWS Redshift" or plain "AWS" (data._aws_label, derived from the
        # services actually ingested), and the nav row beside it reads the same source.
        provider_label(_GROUP),
        scope=scope(),
        breakdown_lead=(_spend_partition,),
        extra_kpis=(_commitment_utilization_kpi, _spectrum_kpi, _active_clusters_kpi),
        attribution_tab=_attribution_section,
        efficiency_tab=_waste_section,
        efficiency_tab_label="Efficiency & Waste",
        # The shared policy view already carries AWS/Redshift entity rows. It must be
        # visible even when a check is not yet measurable: that is an explicit
        # coverage gap, not a clean compliance result.
        show_policy=True,
        extra_tabs=[("Client Driver Health", lambda: driver_health.render("AWS", "Redshift"))],
        show_alerts=False,
        show_daily_trend=False,
        show_credit_kpi=False,
        combine_sku_spend_and_mom=True,
        # Shared categorical actuals and the shared hatched projection marker match
        # Databricks. This operating-spend series intentionally excludes credits.
        monthly_chart_label="Monthly operating cost",
        invoice_explanations_in_trend=True,
    )


def _spectrum_kpi(sm: date, end: date) -> chrome.KpiCard | None:
    """Existing Spectrum invoice charge, never a second S3/storage total."""
    df = gold_df(
        f"SELECT coalesce(sum(net_cost), 0) AS c FROM {_GROUP}.spend_by_cost_subcategory_month "
        f"WHERE service_name IN ({_SERVICE_IN}) AND cost_subcategory = 'spectrum_scan' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    cost = float(df["c"].iloc[0]) if not df.empty else 0.0
    if not cost:
        return None
    return ("Spectrum scans", compact_money(cost), "Included in Net Spend", "volume")


def _commitment_utilization_kpi(sm: date, end: date) -> chrome.KpiCard | None:
    """Latest complete month's used share; an in-progress month is not comparable."""
    if not gold_view_published(_GROUP, "commitment_summary_month"):
        return None
    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    rows = gold_df(
        "SELECT charge_month, commitment_discount_status, sum(effective_cost) AS cost "
        f"FROM {_GROUP}.commitment_summary_month "
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        f"AND charge_month < '{current}' AND commitment_discount_status IN ('Used', 'Unused') "
        "GROUP BY charge_month, commitment_discount_status"
    )
    if rows.empty:
        return None
    latest = pd.Timestamp(rows["charge_month"].max()).date()
    latest_rows = rows[pd.to_datetime(rows["charge_month"]).dt.date == latest]
    used = float(latest_rows.loc[latest_rows["commitment_discount_status"] == "Used", "cost"].sum())
    unused = float(
        latest_rows.loc[latest_rows["commitment_discount_status"] == "Unused", "cost"].sum()
    )
    total = used + unused
    if total <= 0:
        return None
    return (
        "Commitment utilization",
        f"{100 * used / total:.1f}%",
        f"{latest:%b %Y} · {compact_money(used)} used · {compact_money(unused)} unused",
        "volume",
    )


def _active_clusters_kpi(sm: date, end: date) -> chrome.KpiCard | None:
    df = gold_df(
        f"SELECT count(DISTINCT regexp_extract(resource_id, ':cluster:(.+)$', 1)) AS n "
        f"FROM {_GROUP}.resource_month WHERE service_name IN ({_SERVICE_IN}) "
        "AND resource_id LIKE '%:cluster:%' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    count = int(df["n"].iloc[0]) if not df.empty and not pd.isna(df["n"].iloc[0]) else 0
    return ("Billed clusters", f"{count}", "Provisioned clusters in this window", "volume")


def _spend_partition(sm: date, end: date) -> None:
    """Redshift's own Breakdown lead-in: the account-level vs cluster-attributed split,
    then a cluster → SKU drill-through. Genuinely Redshift-shaped — no other provider's
    bill partitions on a reservation-vs-cluster ARN — so it's a lead-in panel rather
    than something pushed into the shared Breakdown tab.
    """
    _in_range = f"charge_month >= '{sm}' AND charge_month <= '{end}'"

    # Split the total into two buckets that strictly partition it — every row is in
    # exactly one, so "committed" + "cluster/SKU invoice" always reconciles to the
    # KPI total above, nothing silently vanishes:
    #  - resource-attributed usage (a real "cluster:<name>"/snapshot/etc ARN, and NOT
    #    a reservation) → the cluster/SKU invoice below.
    #  - everything else → account-level: RI/Savings-Plan commitment charges (their
    #    ResourceId is a "reserved-instances/<uuid>" ARN, never a cluster one — a
    #    reservation isn't tied to one cluster) AND any credits/tax applied against
    #    them (those carry a NULL ResourceId — AWS ties neither side to a resource).
    #    Credits matter here — confirmed against real data: excluding them would
    #    report a materially wrong, misleadingly positive "commitment" number.
    _attributed = "resource_id <> '(none)' AND resource_id NOT LIKE '%:reserved-instances/%'"
    # The other half of the partition: real, resource-attributed usage. The account-level
    # balance itself is promoted to the KPI row, where it is visible beside net spend.
    _invoice_scope = f"WHERE service_name IN ({_SERVICE_IN}) AND {_attributed} AND {_in_range}"

    # The exact same derived bucket is used to roll up the parent rows and filter the
    # child rows. That makes a selected cluster's SKU total an exact reconciliation,
    # including the two non-cluster resource buckets.
    _bucket = (
        "CASE WHEN resource_id LIKE '%:cluster:%' "
        "THEN regexp_extract(resource_id, ':cluster:(.+)$', 1) "
        "WHEN resource_id LIKE '%:snapshot:%' THEN '(snapshot storage)' "
        "ELSE '(other Redshift resource)' END"
    )
    _scoped_rows = (
        "WITH scoped_rows AS ("
        f"SELECT {_bucket} AS cluster, sku_id, sku_description, net_cost "
        f"FROM {_GROUP}.resource_month {_invoice_scope}"
        ") "
    )
    cluster = gold_df(
        _scoped_rows + "SELECT cluster, sum(net_cost) AS net_cost FROM scoped_rows "
        "GROUP BY cluster ORDER BY net_cost DESC"
    )
    if not cluster.empty:
        with chrome.panel():
            @ui.refreshable
            def _drill(selected_cluster: str | None = None) -> None:
                if selected_cluster is None:
                    chrome.panel_title("Spend by cluster")

                    def _open_cluster(row: dict[str, object]) -> None:
                        _drill.refresh(str(row["cluster"]))

                    chrome.searchable_table(
                        cluster,
                        key="redshift_cluster",
                        search_col="cluster",
                        money_cols=["net_cost"],
                        rename={"cluster": "Cluster", "net_cost": "Net cost"},
                        pagination=20,
                        on_row_click=_open_cluster,
                    )
                    return

                ui.button(
                    "All clusters", icon="arrow_back", on_click=lambda: _drill.refresh()
                ).props("flat dense no-caps").style(f"color:{chrome.ACCENT};")
                chrome.panel_title(f"Spend by SKU — {selected_cluster}")
                sku = gold_df(
                    _scoped_rows
                    + "SELECT sku_id, arg_max(sku_description, net_cost) AS description, "
                    "sum(net_cost) AS net_cost FROM scoped_rows "
                    f"WHERE cluster = '{_sql_str(selected_cluster)}' "
                    "GROUP BY sku_id ORDER BY net_cost DESC"
                )
                chrome.searchable_table(
                    sku,
                    key="redshift_cluster_sku",
                    search_col="sku_id",
                    money_cols=["net_cost"],
                    rename={"sku_id": "SKU", "description": "Description", "net_cost": "Net cost"},
                    pagination=20,
                )

            _drill()

    _spectrum_table_allocation(sm, end)

    # Credits, cost subcategory, commitment coverage and invoice reconciliation used to
    # be called from here. They're the shared Breakdown tab's panels now and render
    # immediately below this one — the account-wide three by declaration in
    # _ACCOUNT_WIDE, which is what keeps the credit figure the account-level bucket
    # above nets against identical to the one itemized under it.


def _spectrum_table_allocation(sm: date, end: date) -> None:
    """Show the invoice-reconciled Spectrum allocation, never an S3 storage bill."""
    rows = gold_df(
        "SELECT entity_name AS external_table, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_scan_count') AS BIGINT) AS scans, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_scanned_gb') AS DOUBLE) "
        "AS scanned_gb, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_returned_gb') AS DOUBLE) "
        "AS returned_gb, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_allocated_cost') AS DOUBLE) "
        "AS allocated_cost "
        "FROM efficiency.efficiency_entity_month "
        "WHERE provider_name = 'AWS' AND entity_type = 'table' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "AND json_extract_string(cause_detail, '$.spectrum_allocation_status') = 'allocated' "
        "ORDER BY allocated_cost DESC"
    )
    if rows.empty:
        return
    rows["return_pct"] = (100.0 * rows["returned_gb"] / rows["scanned_gb"]).round(1)
    rows["estimated_recovery"] = (rows["allocated_cost"] * 0.3).round(2)
    chrome.panel_title("Spectrum scan cost by external table")
    chrome.section_caption(
        "Allocated from the existing, target-scoped Redshift Spectrum invoice charge by "
        "measured scanned bytes; this is not an additional S3 storage cost."
    )
    chrome.searchable_table(
        rows[
            [
                "external_table",
                "scans",
                "scanned_gb",
                "return_pct",
                "allocated_cost",
                "estimated_recovery",
            ]
        ],
        key="redshift_spectrum_table_cost",
        search_col="external_table",
        money_cols=["allocated_cost", "estimated_recovery"],
        rename={
            "external_table": "External table", "scans": "Scans", "scanned_gb": "Scanned GB",
            "return_pct": "Returned %", "allocated_cost": "Allocated Spectrum charge",
            "estimated_recovery": "Estimated recovery",
        },
    )


def _attribution_section(sm: date, end: date) -> None:
    """Redshift cluster → invoice component → measured-user attribution.

    This deliberately does *not* use the generic service-first attribution panel:
    the whole /aws page is already Redshift-scoped, so an initial "Amazon Redshift"
    row adds an empty click.  More importantly, users are only an honest estimate for
    compute-like components; storage and Spectrum remain at their billed cluster grain.
    """
    _cluster_attribution(sm, end)
    # The AWS GOLD group is Redshift-scoped by aws_focus's default service filter,
    # so this customer-tag drill has the same bill scope as the cost hierarchy.
    attribution.tag_breakdown(_GROUP, end, sm)


_CLUSTER_COLS = ["cluster_id", "gross_cost"]
_COMPONENT_COLS = ["cost_subcategory", "gross_cost"]
_USER_COLS = ["owner_user", "allocated_cost", "duration_share_pct"]
_COMPUTE_LIKE_COMPONENTS = frozenset({"compute", "concurrency_scaling"})
_UNASSIGNED_CLUSTER = "(not assigned to a cluster)"


@dataclass(frozen=True)
class _ClusterDrill:
    level: str = "cluster"
    cluster_id: str | None = None
    cost_subcategory: str | None = None


def _cluster_breadcrumb(
    *steps: tuple[str, _ClusterDrill | None], refresh: Callable[[_ClusterDrill], object]
) -> None:
    """Small local breadcrumb for the Redshift-only cost hierarchy."""
    with ui.row().classes("items-center gap-2 text-xs"):
        for index, (label, target) in enumerate(steps):
            if index:
                ui.label("/").style(f"color:{chrome.INK_MUTED}")
            if target is None:
                ui.label(label).style(f"color:{chrome.INK_SECONDARY}")
            else:
                ui.button(label, on_click=lambda t=target: refresh(t)).props(
                    "flat dense no-caps"
                ).style(f"color:{chrome.ACCENT};padding:0 2px;min-height:0;")


def _cluster_attribution(sm: date, end: date) -> None:
    """Render one Redshift-only drill: cluster → component → database user."""
    with chrome.panel():
        title = ui.label("Cost attribution — clusters").classes("text-sm font-medium mb-2").style(
            f"color:{chrome.INK_SECONDARY}"
        )
        body = ui.column().classes("w-full gap-2")

        @ui.refreshable
        def _body(state: _ClusterDrill) -> None:
            body.clear()
            with body:
                if not gold_view_published(_GROUP, "redshift_cluster_cost_month"):
                    chrome.section_caption(
                        "Cluster attribution isn't published yet — run `flashlight transform`."
                    )
                    return
                if state.level == "cluster":
                    title.text = "Cost attribution — clusters"
                    _render_cluster_level(sm, end, refresh=_body.refresh)
                elif state.level == "component":
                    assert state.cluster_id is not None
                    title.text = f"Cost attribution — {state.cluster_id}"
                    _render_component_level(state.cluster_id, sm, end, refresh=_body.refresh)
                elif state.level == "user":
                    assert state.cluster_id is not None and state.cost_subcategory is not None
                    title.text = (
                        f"Cost attribution — {state.cluster_id} — {state.cost_subcategory}"
                    )
                    _render_component_users(
                        state.cluster_id, state.cost_subcategory, sm, end, refresh=_body.refresh
                    )
                else:
                    assert state.cluster_id is not None and state.cost_subcategory is not None
                    title.text = (
                        f"Cost attribution — {state.cluster_id} — {state.cost_subcategory}"
                    )
                    _render_component_charges(
                        state.cluster_id, state.cost_subcategory, sm, end, refresh=_body.refresh
                    )

        _body(_ClusterDrill())


def _render_cluster_level(
    sm: date, end: date, *, refresh: Callable[[_ClusterDrill], object]
) -> None:
    """Landing level: every billed Redshift cluster plus an explicit unassigned bucket."""
    rows = gold_df(
        "SELECT cluster_id, sum(gross_cost) AS gross_cost "
        f"FROM {_GROUP}.redshift_cluster_cost_month "
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY cluster_id HAVING sum(gross_cost) <> 0 ORDER BY gross_cost DESC"
    )
    if rows.empty:
        chrome.section_caption("No Redshift charges in range.")
        return

    def _on_click(row: dict[str, object]) -> None:
        cluster_id = str(row["cluster_id"])
        if cluster_id != _UNASSIGNED_CLUSTER:
            refresh(_ClusterDrill(level="component", cluster_id=cluster_id))

    chrome.searchable_table(
        rows[_CLUSTER_COLS],
        key="redshift_attribution_clusters",
        search_col="cluster_id",
        money_cols=["gross_cost"],
        rename={"cluster_id": "Cluster", "gross_cost": "Cost"},
        max_rows=40,
        on_row_click=_on_click,
    )


def _render_component_level(
    cluster_id: str, sm: date, end: date, *, refresh: Callable[[_ClusterDrill], object]
) -> None:
    """Invoice components below one cluster; every component reaches an honest child."""
    _cluster_breadcrumb(("← All clusters", _ClusterDrill()), (cluster_id, None), refresh=refresh)
    cluster = _sql_str(cluster_id)
    rows = gold_df(
        f"SELECT cost_subcategory, sum(gross_cost) AS gross_cost "
        f"FROM {_GROUP}.redshift_cluster_cost_month WHERE cluster_id = '{cluster}' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY cost_subcategory HAVING sum(gross_cost) <> 0 "
        "ORDER BY gross_cost DESC"
    )
    if rows.empty:
        chrome.section_caption("No charge components for this cluster in range.")
        return

    def _on_click(row: dict[str, object]) -> None:
        component = str(row["cost_subcategory"])
        if component in _COMPUTE_LIKE_COMPONENTS:
            refresh(
                _ClusterDrill(
                    level="user", cluster_id=cluster_id, cost_subcategory=component
                )
            )
        else:
            refresh(
                _ClusterDrill(
                    level="charge", cluster_id=cluster_id, cost_subcategory=component
                )
            )

    chrome.searchable_table(
        rows[_COMPONENT_COLS],
        key=f"redshift_attribution_components_{_slug(cluster_id)}",
        search_col="cost_subcategory",
        money_cols=["gross_cost"],
        rename={"cost_subcategory": "Cost component", "gross_cost": "Cost"},
        max_rows=40,
        on_row_click=_on_click,
    )
    chrome.section_caption(
        "Compute and concurrency scaling drill to estimated database users. Storage, "
        "Spectrum, and unclassified charges drill to their billed SKU lines."
    )


def _render_component_charges(
    cluster_id: str,
    cost_subcategory: str,
    sm: date,
    end: date,
    *,
    refresh: Callable[[_ClusterDrill], object],
) -> None:
    """Billed SKU detail where a user allocation would be fabricated."""
    _cluster_breadcrumb(
        ("← All clusters", _ClusterDrill()),
        (cluster_id, _ClusterDrill(level="component", cluster_id=cluster_id)),
        (cost_subcategory, None),
        refresh=refresh,
    )
    if cost_subcategory == "spectrum_scan":
        _render_spectrum_table_attribution(cluster_id, sm, end)
        return
    chrome.caption_info(
        "Billed SKU detail — no user allocation is inferred for this cost component.",
        "Storage, Spectrum, and unclassified Redshift charges are real cluster costs, "
        "but AWS billing does not provide a defensible per-user allocation basis for them.",
    )
    cluster = _sql_str(cluster_id)
    component = _sql_str(cost_subcategory)
    rows = gold_df(
        "SELECT sku_id, arg_max(sku_description, gross_cost) AS sku_description, "
        "sum(gross_cost) AS gross_cost "
        f"FROM {_GROUP}.redshift_cluster_cost_month "
        f"WHERE cluster_id = '{cluster}' AND cost_subcategory = '{component}' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "GROUP BY sku_id ORDER BY gross_cost DESC"
    )
    if rows.empty:
        chrome.section_caption("No billed SKU detail for this component in range.")
        return
    chrome.searchable_table(
        rows,
        key=f"redshift_attribution_charges_{_slug(cluster_id)}_{_slug(cost_subcategory)}",
        search_col="sku_id",
        money_cols=["gross_cost"],
        rename={"sku_id": "SKU", "sku_description": "Description", "gross_cost": "Cost"},
        max_rows=40,
    )


def _render_spectrum_table_attribution(cluster_id: str, sm: date, end: date) -> None:
    """Rank external tables by their reconciled share of one cluster's Spectrum bill."""
    cluster = _sql_str(cluster_id)
    rows = gold_df(
        "SELECT entity_name AS external_table, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_scan_count') AS BIGINT) AS scans, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_scanned_gb') AS DOUBLE) "
        "AS scanned_gb, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_returned_gb') AS DOUBLE) "
        "AS returned_gb, "
        "try_cast(json_extract_string(cause_detail, '$.spectrum_allocated_cost') AS DOUBLE) "
        "AS allocated_cost "
        "FROM efficiency.efficiency_entity_month "
        "WHERE provider_name = 'AWS' AND entity_type = 'table' "
        f"AND entity_id LIKE '{cluster}:spectrum:%' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "AND json_extract_string(cause_detail, '$.spectrum_allocation_status') = 'allocated' "
        "ORDER BY allocated_cost DESC"
    )
    if rows.empty:
        measured = gold_df(
            "SELECT count(*) AS n FROM efficiency.efficiency_entity_month "
            "WHERE provider_name = 'AWS' AND entity_type = 'table' "
            f"AND entity_id LIKE '{cluster}:spectrum:%' "
            f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
        )
        has_measurement = not measured.empty and int(measured["n"].iloc[0]) > 0
        if has_measurement:
            chrome.section_caption(
                "Spectrum table telemetry exists, but it does not cover a complete billing "
                "window, so the charge cannot be safely allocated yet. Re-ingest after the "
                "month closes."
            )
        else:
            history = gold_df(
                "SELECT count(*) AS telemetry_months, count(*) FILTER (WHERE "
                "json_extract_string(cause_detail, '$.activity_window_unmeasurable') = 'true') "
                "AS retention_gaps FROM efficiency.efficiency_entity_month "
                "WHERE provider_name = 'AWS' AND entity_type = 'sql_warehouse' "
                f"AND entity_id = '{cluster}' "
                f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
            )
            retention_gap = not history.empty and int(history["retention_gaps"].iloc[0]) > 0
            if retention_gap:
                chrome.section_caption(
                    "No table-level Spectrum cost for this selected range: Redshift query "
                    "history did not retain its full start date when telemetry was collected. "
                    "The connector is active, but table costs cannot be safely inferred from "
                    "a partial scan-history window. Choose a recent completed period or retain "
                    "query history externally for longer-term attribution."
                )
            else:
                chrome.section_caption(
                    "No Spectrum table telemetry for this cluster. Confirm the Redshift connector "
                    "can read `SVL_S3QUERY_SUMMARY`, then run `flashlight ingest`; table costs "
                    "are not inferred from the SKU alone."
                )
        return

    rows["return_pct"] = (100.0 * rows["returned_gb"] / rows["scanned_gb"]).round(1)
    chrome.caption_info(
        "External-table cost estimate, allocated by each table's measured share of scanned bytes.",
        "The allocated table costs reconcile to this cluster's billed Spectrum scan charge. "
        "This is not an additional S3 storage cost; a low returned percentage can indicate "
        "poor partition pruning.",
    )
    chrome.searchable_table(
        rows[["external_table", "scans", "scanned_gb", "return_pct", "allocated_cost"]],
        key=f"redshift_attribution_spectrum_{_slug(cluster_id)}",
        search_col="external_table",
        money_cols=["allocated_cost"],
        rename={
            "external_table": "External table",
            "scans": "Scans",
            "scanned_gb": "Scanned GB",
            "return_pct": "Returned %",
            "allocated_cost": "Allocated Spectrum charge",
        },
        max_rows=40,
    )


def _render_component_users(
    cluster_id: str,
    cost_subcategory: str,
    sm: date,
    end: date,
    *,
    refresh: Callable[[_ClusterDrill], object],
) -> None:
    """Allocate one compute-like component by measured query-duration share only."""
    _cluster_breadcrumb(
        ("← All clusters", _ClusterDrill()),
        (cluster_id, _ClusterDrill(level="component", cluster_id=cluster_id)),
        (cost_subcategory, None),
        refresh=refresh,
    )
    chrome.caption_info(
        "Estimated from database-query duration; months without telemetry remain unallocated.",
        "Each measured user's duration share is applied only to this cluster's billed "
        f"{cost_subcategory.replace('_', ' ')} charge. Storage and Spectrum are never "
        "forced into this user allocation.",
    )
    cluster = _sql_str(cluster_id)
    component = _sql_str(cost_subcategory)
    rows = gold_df(
        "WITH component_cost AS ("
        " SELECT charge_month, sum(gross_cost) AS component_cost "
        f" FROM {_GROUP}.redshift_cluster_cost_month "
        f" WHERE cluster_id = '{cluster}' AND cost_subcategory = '{component}' "
        f" AND charge_month >= '{sm}' AND charge_month <= '{end}' GROUP BY charge_month"
        "), raw_user_share AS ("
        " SELECT charge_month, owner_user, max(primary_signal_value) / 100.0 AS duration_share "
        " FROM efficiency.utilization_entity_month "
        " WHERE provider_name = 'AWS' AND entity_type = 'sql_warehouse_user' "
        f" AND entity_id LIKE '{cluster}:%' "
        f" AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        " GROUP BY charge_month, owner_user"
        "), user_share AS ("
        " SELECT *, sum(duration_share) OVER (PARTITION BY charge_month) AS total_duration_share "
        " FROM raw_user_share"
        "), allocations AS ("
        " SELECT u.owner_user, c.component_cost * u.duration_share / "
        " nullif(u.total_duration_share, 0) AS allocated_cost, u.duration_share "
        " FROM component_cost c JOIN user_share u USING (charge_month)"
        "), user_totals AS ("
        " SELECT owner_user, sum(allocated_cost) AS allocated_cost, "
        " 100.0 * sum(allocated_cost) / "
        " nullif((SELECT sum(component_cost) FROM component_cost), 0) "
        " AS duration_share_pct FROM allocations GROUP BY owner_user"
        "), remainder AS ("
        " SELECT 'Unallocated (no query telemetry)' AS owner_user, "
        " greatest(0, coalesce((SELECT sum(component_cost) FROM component_cost), 0) - "
        " coalesce((SELECT sum(allocated_cost) FROM allocations), 0)) AS allocated_cost, "
        " NULL::DOUBLE AS duration_share_pct"
        ") SELECT * FROM user_totals UNION ALL SELECT * FROM remainder "
        " ORDER BY allocated_cost DESC"
    )
    if rows.empty:
        chrome.section_caption("No billable component cost in range.")
        return
    chrome.searchable_table(
        rows[_USER_COLS],
        key=f"redshift_attribution_users_{_slug(cluster_id)}_{_slug(cost_subcategory)}",
        search_col="owner_user",
        money_cols=["allocated_cost"],
        pct_cols=["duration_share_pct"],
        rename={
            "owner_user": "Database user",
            "allocated_cost": "Estimated cost",
            "duration_share_pct": "Query-duration share",
        },
        max_rows=40,
    )
    if cost_subcategory == "compute":
        _render_compute_heavy_tables(cluster_id, sm, end)


def _render_compute_heavy_tables(cluster_id: str, sm: date, end: date) -> None:
    """Recent main-cluster tables ranked by query-time weighted scan share.

    STL_SCAN excludes concurrency-scaling queries and has finite retention, so the
    table rows are workload evidence only.  The billed compute allocation above stays
    at the user grain, where the measured billing-period share actually exists.
    """
    cluster = _sql_str(cluster_id)
    sql = (
        "SELECT entity_name AS table_name, owner_user, "
        "try_cast(json_extract_string(cause_detail, '$.table_weighted_exec_seconds') AS DOUBLE) "
        "AS weighted_exec_seconds, "
        "try_cast(json_extract_string(cause_detail, '$.table_compute_share_pct') AS DOUBLE) "
        "AS compute_share_pct, "
        "try_cast(json_extract_string(cause_detail, '$.table_scan_gb') AS DOUBLE) AS scan_gb, "
        "activity_count AS query_count, "
        "try_cast(json_extract_string(cause_detail, '$.table_rows_pre_filter') AS DOUBLE) "
        "AS rows_pre_filter, "
        "try_cast(json_extract_string(cause_detail, '$.table_rows_returned') AS DOUBLE) "
        "AS rows_returned "
        "FROM efficiency.efficiency_entity_month "
        "WHERE provider_name = 'AWS' AND entity_type = 'table' "
        f"AND entity_id LIKE '{cluster}:%' "
        f"AND entity_id NOT LIKE '{cluster}:spectrum:%' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
        "AND try_cast(json_extract_string(cause_detail, "
        "'$.table_weighted_exec_seconds') AS DOUBLE) "
        "IS NOT NULL ORDER BY weighted_exec_seconds DESC"
    )
    try:
        rows = gold_df(sql)
    except Exception:  # noqa: BLE001 - old published GOLD lacks owner_user until transformed
        # The new view includes owner_user, but a running dashboard can still have a
        # pre-upgrade Parquet file registered. Keep the drill usable during that one
        # refresh cycle instead of turning a new optional column into a page failure.
        legacy_sql = (
            sql.replace(
                "entity_name AS table_name, owner_user, ",
                "entity_name AS table_name, NULL::VARCHAR AS owner_user, ",
            )
            .replace(
                "activity_count AS query_count, ",
                "NULL::BIGINT AS query_count, ",
            )
        )
        rows = gold_df(legacy_sql)
    if rows.empty:
        chrome.section_caption(
            "No retained table-to-query workload telemetry. Re-run the Redshift connector "
            "with access to STL_SCAN and STL_WLM_QUERY; this view does not estimate table "
            "compute from table size alone."
        )
        return
    rows["return_pct"] = (100.0 * rows["rows_returned"] / rows["rows_pre_filter"]).round(1)
    chrome.caption_info(
        "Compute-heavy tables from retained main-cluster query history.",
        "A query's execution time is split among its scanned tables by scan bytes (then "
        "pre-filter rows). It is a workload ranking, not a per-table invoice allocation: "
        "STL_SCAN does not include concurrency-scaling queries and its retention is limited.",
    )
    chrome.searchable_table(
        rows[
            [
                "table_name",
                "owner_user",
                "weighted_exec_seconds",
                "compute_share_pct",
                "scan_gb",
                "query_count",
                "return_pct",
            ]
        ],
        key=f"redshift_attribution_compute_tables_{_slug(cluster_id)}",
        search_col="table_name",
        pct_cols=["compute_share_pct", "return_pct"],
        rename={
            "table_name": "Table",
            "owner_user": "Owner",
            "weighted_exec_seconds": "Weighted execution seconds",
            "compute_share_pct": "Workload share",
            "scan_gb": "Scanned GB",
            "query_count": "Queries",
            "return_pct": "Rows returned",
        },
        max_rows=40,
    )


def _tags_section(sm: date, end: date) -> None:
    """Spend-by-tag, scoped to Redshift like the rest of this page. There's no
    per-service tag view, so this scopes ``spend_by_sku_tag_month`` (which carries
    ``sku_id`` but not ``service_name``) down to the SKU ids Redshift's own
    resource rows use — same trick as ``_invoice_scope`` above, one level up.

    Keys are folded (case/separator only, same as ``036_gold_tag_keys.sql`` and
    ``attribution.tag_keys``) rather than grouped on the raw ``tag_key``: this view
    has no normalized-key column of its own to borrow (``spend_by_tag_key_month`` carries
    one but isn't SKU-scoped), so the fold is applied inline instead. Without it, picking
    "team" here could silently miss dollars raw-tagged "Team".
    """
    chrome.panel_title("Spend by tag value")
    _in_range = f"charge_month >= '{sm}' AND charge_month <= '{end}'"
    _fold = "replace(lower(trim(tag_key)), '-', '_')"
    keys = gold_df(
        f"SELECT {_fold} AS tag_key_normalized, sum(net_cost) AS net "
        f"FROM {_GROUP}.spend_by_sku_tag_month "
        f"WHERE {_SKU_SCOPE} AND {_in_range} GROUP BY {_fold} ORDER BY net DESC"
    )
    if keys.empty:
        ui.label("No tagged Redshift spend in range.").classes("text-sm").style(
            f"color:{chrome.INK_MUTED}"
        )
        return

    options = keys["tag_key_normalized"].tolist()
    default = "team" if "team" in options else options[0]
    body_container = ui.column().classes("w-full gap-4")

    @ui.refreshable
    def _tag_values(sel: str) -> None:
        body_container.clear()
        with body_container:
            tags = gold_df(
                f"SELECT tag_value, sum(net_cost) AS net_cost "
                f"FROM {_GROUP}.spend_by_sku_tag_month "
                f"WHERE {_fold} = '{_sql_str(sel)}' AND {_SKU_SCOPE} AND {_in_range} "
                "GROUP BY tag_value ORDER BY net_cost DESC LIMIT 20"
            )
            if tags.empty:
                ui.label("No values for this tag in range.").classes("text-sm").style(
                    f"color:{chrome.INK_MUTED}"
                )
                return
            chrome.searchable_table(
                tags,
                key="redshift_tags",
                search_col="tag_value",
                money_cols=["net_cost"],
                rename={"tag_value": sel, "net_cost": "Net cost"},
            )

    (
        ui.select(options=options, value=default, on_change=lambda e: _tag_values.refresh(e.value))
        .props("dense outlined")
        .classes("w-48")
        .style(f"color:{chrome.INK_PRIMARY}")
    )
    _tag_values(default)


def _cost_cluster_ids() -> set[str]:
    """Real cluster identities visible in the AWS bill (not the snapshot/
    other buckets `_breakdown_section`'s own table lumps separately) — the universe of
    clusters that *could* have optimization telemetry, whether or not they do.
    """
    df = gold_df(
        "SELECT DISTINCT regexp_extract(resource_id, ':cluster:(.+)$', 1) AS cluster "
        f"FROM {_GROUP}.resource_month WHERE service_name IN ({_SERVICE_IN}) "
        "AND resource_id LIKE '%:cluster:%'"
    )
    return set(df["cluster"]) if not df.empty else set()


def _telemetry_cluster_ids() -> list[str]:
    """Clusters an actual `redshift` connector entry has pulled telemetry for —
    entity_id for entity_type='sql_warehouse' under provider AWS is the cluster
    identifier itself (see ingest/connectors/redshift.py). Not filtered by
    x_source_connector: a Redshift connector entry's ``name`` (and so its
    x_source_connector stamp) is whatever connections.yml names it (e.g.
    "Prod"/"BI" for multiple clusters, see effective_connector_name) — never
    literally "redshift" once more than one cluster is configured. provider_name
    ='AWS' + entity_type='sql_warehouse' is already Redshift-only (see module
    docstring), so no further connector-name filter is needed.
    """
    df = gold_df(
        "SELECT DISTINCT entity_id FROM efficiency.efficiency_entity_month "
        f"WHERE provider_name = '{_PROVIDER}' "
        "AND entity_type = 'sql_warehouse' ORDER BY entity_id"
    )
    return list(df["entity_id"]) if not df.empty else []


def _waste_section(sm: date, end: date) -> None:
    chrome.section_title("Efficiency & waste")

    # The bill is the primary cluster universe: a reader needs an answer for all four
    # billed clusters, not only the subset that already has a telemetry connector. A
    # telemetry-only cluster is retained too, so a newly configured connector never
    # vanishes simply because its cost ARN was not present in this billing slice.
    telemetry_clusters = set(_telemetry_cluster_ids())
    cost_clusters = _cost_cluster_ids()
    clusters = sorted(cost_clusters | telemetry_clusters)
    if not clusters:
        (
            ui.label("No billed Redshift clusters or efficiency telemetry yet.")
            .classes("text-sm")
            .style(f"color:{chrome.INK_MUTED}")
        )
        return

    if len(clusters) == 1:
        _cluster_efficiency_section(clusters[0], clusters[0] in telemetry_clusters, sm, end)
    else:
        with ui.tabs().classes("w-full") as tabs:
            tab_refs = [ui.tab(cluster_id) for cluster_id in clusters]
        with (
            ui.tab_panels(tabs, value=tab_refs[0])
            .classes("w-full")
            .style("background:transparent;")
        ):
            for cluster_id, tab_ref in zip(clusters, tab_refs, strict=True):
                with ui.tab_panel(tab_ref):
                    _cluster_efficiency_section(
                        cluster_id, cluster_id in telemetry_clusters, sm, end
                    )


def _cluster_efficiency_section(
    cluster_id: str, instrumented: bool, sm: date, end: date
) -> None:
    """Render one billed cluster's complete efficiency answer or its precise gap."""
    ui.label(f"Cluster: {cluster_id}").classes("text-base font-semibold mt-4").style(
        f"color:{chrome.INK_PRIMARY}"
    )
    _cluster_billing_context(cluster_id, sm, end)
    if not instrumented:
        with chrome.panel():
            chrome.panel_title("Not yet instrumented")
            chrome.section_caption(
                "This cluster has billed Redshift spend, but no `redshift` connector entry. "
                "Add one connection for this cluster in connections.yml to enable its "
                "Efficiency & Waste findings."
            )
        return

    current_month = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    scope = (
        f"(entity_id = '{_sql_str(cluster_id)}' "
        f"OR starts_with(entity_id, '{_sql_str(cluster_id)}:'))"
    )
    telemetry_months = gold_df(
        "SELECT DISTINCT charge_month FROM efficiency.efficiency_entity_month "
        f"WHERE provider_name = '{_PROVIDER}' AND {scope}"
    )
    completed_months = efficiency_waste.completed_record_months(telemetry_months, current_month)
    if not completed_months:
        chrome.empty_state(
            "calendar_month",
            "No completed efficiency month yet",
            "This cluster has telemetry, but its current month is still accruing. Findings "
            "appear after the month closes so partial-month costs do not distort savings.",
        )
        return
    _cluster_waste_section(cluster_id, completed_months[-1])


def _cluster_billing_context(cluster_id: str, sm: date, end: date) -> None:
    """Useful cost context for every billed cluster, even before telemetry exists.

    Cost can show where the money lands, but it cannot honestly classify waste. Keeping
    this separate from the telemetry-derived findings gives an uninstrumented cluster a
    useful first screen without presenting invoice data as an optimization verdict.
    """
    cluster = _sql_str(cluster_id)
    where = (
        f"regexp_extract(resource_id, ':cluster:(.+)$', 1) = '{cluster}' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    summary = gold_df(
        f"SELECT coalesce(sum(net_cost), 0) AS net_cost, "
        f"count(DISTINCT charge_month) AS billed_months "
        f"FROM {_GROUP}.resource_month WHERE {where}"
    )
    net_cost = float(summary["net_cost"].iloc[0]) if not summary.empty else 0.0
    billed_months = int(summary["billed_months"].iloc[0]) if not summary.empty else 0
    chrome.kpi_row(
        [
            ("Billed spend", compact_money(net_cost), "Selected date range", "volume"),
            ("Billed months", str(billed_months), "Cost coverage", "volume"),
        ],
        columns=2,
    )
    sku = gold_df(
        f"SELECT sku_description, sum(net_cost) AS net_cost FROM {_GROUP}.resource_month "
        f"WHERE {where} GROUP BY sku_description ORDER BY net_cost DESC LIMIT 5"
    )
    if sku.empty:
        return
    with chrome.panel():
        chrome.panel_title("What this cluster is billed for")
        chrome.section_caption(
            "Invoice context only — efficiency and waste findings require Redshift telemetry."
        )
        chrome.flat_table(
            sku,
            key=f"redshift_cluster_billing_{_slug(cluster_id)}",
            money_cols=["net_cost"],
            rename={"sku_description": "Billed item", "net_cost": "Net cost"},
        )


def _cluster_waste_section(cluster_id: str, month: str) -> None:
    scope = (
        f"(entity_id = '{_sql_str(cluster_id)}' "
        f"OR starts_with(entity_id, '{_sql_str(cluster_id)}:'))"
    )
    coverage = gold_df(
        "SELECT DISTINCT charge_month, entity_type FROM efficiency.efficiency_entity_month "
        f"WHERE provider_name = '{_PROVIDER}' AND {scope} AND charge_month = '{month}'"
    )
    if coverage.empty:
        return  # shouldn't happen — cluster_id came from this same view

    measured_types = set(coverage.loc[coverage["charge_month"].astype(str) == month, "entity_type"])

    records = gold_df(
        f"SELECT * FROM efficiency.waste_record WHERE provider_name = '{_PROVIDER}' "
        f"AND {scope} AND charge_month = '{month}' ORDER BY recoverable_cost DESC"
    )

    action_groups = action_group_rows(records)
    waste_total = action_groups.loc[action_groups["lens"] == "WASTE", "potential_savings"].sum()
    opp_total = action_groups.loc[action_groups["lens"] == "OPPORTUNITY", "potential_savings"].sum()
    chrome.kpi_row(
        [
            (
                "Waste (recoverable)",
                compact_money(float(waste_total)),
                "Idle time, underutilized capacity",
                "unattributed",
            ),
            (
                "Opportunity (recoverable)",
                compact_money(float(opp_total)),
                "Workloads movable to cheaper compute",
                "decrease",
            ),
        ],
        columns=2,
    )

    # Same "what didn't we measure?" statement every other provider's Efficiency & Waste
    # tab leads with, passed this cluster's own scope so it agrees with the panels around
    # it. On real data this reads "0 of N measured" — Redshift's telemetry is per table and
    # per query shape, neither of which has a per-entity utilization reading.
    efficiency_waste.coverage_caption(_PROVIDER, scope_sql=scope)

    # Compact diagnostic summary; the action queue below is the only table for this
    # cluster, drilled workload/remedy → entity → evidence just like Attribution.
    efficiency_waste.coverage_summary(
        _PROVIDER,
        records,
        measured_types,
        scope_note="for this cluster",
    )
    tracking = gold_df(
        "SELECT * FROM efficiency.waste_resolution_month "
        f"WHERE provider_name = '{_PROVIDER}' AND {scope}"
    ) if gold_view_published("efficiency", "waste_resolution_month") else pd.DataFrame()
    efficiency_waste.action_queue(
        records,
        tracking,
        key=f"redshift_actions_{_slug(cluster_id)}",
    )
