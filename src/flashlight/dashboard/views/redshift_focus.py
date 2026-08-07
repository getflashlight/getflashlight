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
- **Attribution**: :func:`_attribution_section`. Tag-key ranking is account-wide (no
  ``service_name`` on those views); untagged infrastructure (service→resource→driver)
  and the tag-value drill are Redshift-scoped — the former via ``service_name``, the
  latter via ``sku_id`` on ``spend_by_sku_tag_month``.
- **Efficiency & Waste**: faceted per cluster (:func:`_waste_section`) — one section per
  Redshift cluster that has efficiency telemetry (``entity_id`` for
  ``entity_type='sql_warehouse'`` under ``provider_name='AWS'`` is the cluster
  identifier itself, and every other Redshift entity_type is ``<cluster_id>:...``
  prefixed, see ``ingest/connectors/redshift.py``). Clusters that bill on this
  account (visible in the cost section's "Spend by cluster") but have no
  ``redshift`` connector entry configured are listed separately, not silently
  omitted. Each cluster's findings come from ``efficiency.waste_record`` filtered
  to ``provider_name = 'AWS'`` plus either a ``redshift_``-prefixed category
  (Redshift-only rules) or ``entity_type IN ('sql_warehouse', 'sql_warehouse_user')``
  — the two categories shared with Databricks (``idle``,
  ``sql_warehouse_user_concentration``) key off those entity types, and under
  ``provider_name = 'AWS'`` only the Redshift connector emits them (S3's own signal
  uses ``entity_type = 'storage'``), so the combination stays Redshift-only despite
  ``entity_type`` names being shared in *name* with Databricks' own entity type
  (just under a different ``provider_name``). ``efficiency_waste.rule_coverage_table``
  also cross-references ``efficiency.efficiency_entity_month`` (every rule this connector
  evaluates, not just the ones that fired) so a rule that found nothing reads
  differently from one whose telemetry never arrived this window. That table used to live
  here, driven by a hand-maintained rule→group map; it's derived from the rule pool now
  (``waste_rules.coverage_groups``) and renders on every provider's tab.

  This tab is why the page does NOT also carry ``efficiency_waste.render()`` the way every
  other provider page does: ``_waste_section`` is scoped per *cluster*, which is finer than
  per provider, so a provider-scoped tab beside it would render the union of these sections
  — the same ``waste_record`` rows twice. What it does borrow is
  ``efficiency_waste.coverage_caption``, passed the same per-cluster ``scope`` predicate, so
  the "what didn't we measure?" statement is cluster-scoped like everything around it.
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published, provider_label
from flashlight.dashboard.theme import compact_money
from flashlight.dashboard.views import attribution, efficiency_waste, provider_focus
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
        scope_caption=_scope_caption,
        breakdown_lead=(_spend_partition,),
        attribution_tab=_attribution_section,
        efficiency_tab=_waste_section,
    )


def _scope_caption(sm: date, end: date) -> None:
    """Name the narrowing — and the spend it therefore leaves out.

    The hidden-spend line matters because the scope is a hardcoded service list, not a
    reflection of what was ingested: widen ``include_services`` in connections.yml and
    the extra spend lands in ``aws.*`` but appears on no page at all. Saying so is the
    difference between a scoped page and a quietly incomplete one.

    Amazon S3 and Amazon Elastic Compute Cloud are special: bronze still pulls them for
    the storage/compute planes, but ``silver.focus_provider_bill`` keeps both out of
    ``aws.*`` GOLD entirely, so neither will ever show up in the hidden-services query
    below. Point at Databricks Storage / Databricks Compute when those planes have
    dollars for this window.
    """
    chrome.section_caption(
        "Scoped from the AWS bill to Redshift's own FOCUS service names: "
        f"{', '.join(sorted(REDSHIFT_SERVICE_NAMES))}."
    )
    storage_note = ""
    if gold_view_published("storage", "backing_storage_month"):
        s3 = gold_df(
            "SELECT coalesce(sum(net_cost), 0) AS c FROM storage.backing_storage_month "
            f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
        )
        if not s3.empty and float(s3["c"].iloc[0]):
            storage_note = (
                " Amazon S3 is ingested for Databricks Storage "
                "(see Databricks → Databricks Storage); it is not in aws.* GOLD."
            )
    compute_note = ""
    if gold_view_published("compute", "backing_compute_month"):
        ec2 = gold_df(
            "SELECT coalesce(sum(net_cost), 0) AS c FROM compute.backing_compute_month "
            f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
        )
        if not ec2.empty and float(ec2["c"].iloc[0]):
            compute_note = (
                " Amazon EC2 is ingested for Databricks Compute "
                "(see Databricks → Databricks Compute); it is not in aws.* GOLD."
            )

    hidden = gold_df(
        "SELECT service_name, sum(net_cost) AS net_cost "
        f"FROM {_GROUP}.spend_by_service_month "
        f"WHERE charge_month >= '{sm}' AND charge_month <= '{end}' "
        f"AND service_name NOT IN ({_SERVICE_IN}) "
        "GROUP BY service_name HAVING sum(net_cost) <> 0 ORDER BY sum(net_cost) DESC"
    )
    if hidden.empty:
        note = (storage_note + compute_note).strip()
        if note:
            chrome.section_caption(note)
        return
    total = float(hidden["net_cost"].sum())
    names = ", ".join(str(s) for s in hidden["service_name"].head(5))
    more = f" and {len(hidden) - 5} more" if len(hidden) > 5 else ""
    chrome.section_caption(
        f"This page hides {compact_money(total)} of other AWS spend in this window "
        f"({names}{more}) — it is outside every figure below. The `aws_focus` connector "
        "ingests only `include_services`, so a widened service list lands in the lake "
        f"and in Home's AWS total, but not on this page.{storage_note}{compute_note}"
    )


def _spend_partition(sm: date, end: date) -> None:
    """Redshift's own Breakdown lead-in: the account-level vs cluster-attributed split,
    then spend by cluster and by SKU. Genuinely Redshift-shaped — no other provider's
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
    _attributed = (
        "resource_id <> '(none)' AND resource_id NOT LIKE '%:reserved-instances/%'"
    )
    committed = gold_df(
        f"SELECT sum(net_cost) AS net_cost FROM {_GROUP}.resource_month "
        f"WHERE service_name IN ({_SERVICE_IN}) AND NOT ({_attributed}) AND {_in_range}"
    )
    committed_cost = float(committed["net_cost"].iloc[0]) if not committed.empty else 0.0
    if committed_cost:
        with chrome.panel():
            chrome.panel_title("Account-level (not tied to a cluster)")
            ui.label(compact_money(committed_cost)).classes("text-2xl font-semibold")
            chrome.section_caption(
                "Reserved Instance / Savings Plan commitment charges, and any credits "
                "applied against them — AWS doesn't tie either side to one cluster. "
                "This is what the account committed to and was credited, not what any "
                "cluster actually used; a negative number means credits outweighed "
                "unused-commitment charges this window. See cluster usage below."
            )

    # The other half of the partition above: real, resource-attributed usage.
    _invoice_scope = f"WHERE service_name IN ({_SERVICE_IN}) AND {_attributed} AND {_in_range}"

    cluster = gold_df(
        "SELECT CASE WHEN resource_id LIKE '%:cluster:%' "
        "THEN regexp_extract(resource_id, ':cluster:(.+)$', 1) "
        "WHEN resource_id LIKE '%:snapshot:%' THEN '(snapshot storage)' "
        "WHEN resource_id LIKE '%-serverless:%' THEN '(Serverless workgroup)' "
        "ELSE '(other Redshift resource)' END AS cluster, "
        f"sum(net_cost) AS net_cost FROM {_GROUP}.resource_month {_invoice_scope} "
        "GROUP BY 1 ORDER BY net_cost DESC"
    )
    if not cluster.empty:
        with chrome.panel():
            chrome.panel_title("Spend by cluster")
            chrome.section_caption(
                "Usage billed directly to a cluster — node-hours, storage, data scanned, "
                "concurrency scaling. Excludes RI commitments and credits (see above)."
            )
            chrome.flat_table(
                cluster, key="redshift_cluster", money_cols=["net_cost"],
                rename={"cluster": "Cluster", "net_cost": "Net cost"},
            )

    sku = gold_df(
        "SELECT sku_id, arg_max(sku_description, net_cost) AS description, "
        f"sum(net_cost) AS net_cost FROM {_GROUP}.resource_month {_invoice_scope} "
        "GROUP BY sku_id ORDER BY net_cost DESC"
    )
    if not sku.empty:
        with chrome.panel():
            chrome.panel_title("Spend by SKU")
            chrome.flat_table(
                sku, key="redshift_sku", money_cols=["net_cost"],
                rename={"sku_id": "SKU", "description": "Description", "net_cost": "Net cost"},
            )

    # Credits, cost subcategory, commitment coverage and invoice reconciliation used to
    # be called from here. They're the shared Breakdown tab's panels now and render
    # immediately below this one — the account-wide three by declaration in
    # _ACCOUNT_WIDE, which is what keeps the credit figure the account-level bucket
    # above nets against identical to the one itemized under it.


def _attribution_section(sm: date, end: date) -> None:
    """Attribution for the AWS account this page's Redshift spend bills to.

    Untagged infrastructure (the service→resource→driver drill) is Redshift-scoped via
    ``service_name`` — Redshift's own service names are in ``attribution``'s
    ``shared_subgrain`` tier, so a cluster's untagged spend still drills into its
    estimated per-user drivers. Tag-key ranking is account-wide. Tag-value drill is
    this page's own SKU-scoped :func:`_tags_section`.
    """
    chrome.section_caption(
        "Tag-key ranking is account-wide (no service dimension on that view). "
        "Untagged infrastructure and the tag-value panel are Redshift-scoped. "
        "In practice the whole bill is Redshift anyway — aws_focus ingests only "
        "include_services, Redshift by default."
    )
    attribution.untagged_infrastructure(
        _GROUP, end, sm, scope_sql=scope().predicate("spend_untagged_by_service_month")
    )
    attribution.tag_keys(_GROUP, end, sm)
    with chrome.panel():
        _tags_section(sm, end)


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
                tags, key="redshift_tags", search_col="tag_value",
                money_cols=["net_cost"], rename={"tag_value": sel, "net_cost": "Net cost"},
            )

    (
        ui.select(options=options, value=default, on_change=lambda e: _tag_values.refresh(e.value))
        .props("dense outlined")
        .classes("w-48")
        .style(f"color:{chrome.INK_PRIMARY}")
    )
    _tag_values(default)


def _cost_cluster_ids() -> set[str]:
    """Real cluster identities visible in the AWS bill (not the snapshot/serverless/
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


def _waste_section() -> None:
    chrome.section_title("Redshift optimization")

    clusters = _telemetry_cluster_ids()
    if not clusters:
        cost_clusters = _cost_cluster_ids()
        if cost_clusters:
            chrome.section_caption(
                f"{len(cost_clusters)} Redshift cluster(s) bill on this account "
                f"({', '.join(sorted(cost_clusters))}) but none has a `redshift` "
                "connector entry configured — add one per cluster to connections.yml "
                "to enable waste/opportunity detection."
            )
        else:
            ui.label("No Redshift waste/optimization signals yet.").classes("text-sm").style(
                f"color:{chrome.INK_MUTED}"
            )
        return

    if len(clusters) == 1:
        _cluster_waste_section(clusters[0])
    else:
        with ui.tabs().classes("w-full") as tabs:
            tab_refs = [ui.tab(cluster_id) for cluster_id in clusters]
        with ui.tab_panels(tabs, value=tab_refs[0]).classes("w-full").style(
            "background:transparent;"
        ):
            for cluster_id, tab_ref in zip(clusters, tab_refs, strict=True):
                with ui.tab_panel(tab_ref):
                    _cluster_waste_section(cluster_id)

    uninstrumented = sorted(_cost_cluster_ids() - set(clusters))
    if uninstrumented:
        with chrome.panel():
            chrome.panel_title("Not yet instrumented")
            chrome.section_caption(
                "These clusters bill on this AWS account (cost is visible above) but "
                "have no `redshift` connector entry — waste/opportunity detection "
                "needs one per cluster in connections.yml."
            )
            for cluster_id in uninstrumented:
                ui.label(f"· {cluster_id}").classes("text-sm").style(
                    f"color:{chrome.INK_SECONDARY}"
                )


def _cluster_waste_section(cluster_id: str) -> None:
    scope = (
        f"(entity_id = '{_sql_str(cluster_id)}' "
        f"OR starts_with(entity_id, '{_sql_str(cluster_id)}:'))"
    )
    coverage = gold_df(
        "SELECT DISTINCT charge_month, entity_type FROM efficiency.efficiency_entity_month "
        f"WHERE provider_name = '{_PROVIDER}' AND {scope}"
    )
    if coverage.empty:
        return  # shouldn't happen — cluster_id came from this same view

    month = str(sorted(coverage["charge_month"].astype(str).unique())[-1])
    measured_types = set(
        coverage.loc[coverage["charge_month"].astype(str) == month, "entity_type"]
    )

    records = gold_df(
        f"SELECT * FROM efficiency.waste_record WHERE provider_name = '{_PROVIDER}' "
        f"AND {scope} AND charge_month = '{month}' ORDER BY recoverable_cost DESC"
    )

    ui.label(f"Cluster: {cluster_id}").classes("text-base font-semibold mt-4").style(
        f"color:{chrome.INK_PRIMARY}"
    )
    month_label = pd.Timestamp(month).strftime("%b %Y")
    chrome.section_caption(f"Showing {month_label} — the latest month with telemetry.")

    waste_total = (
        records.loc[records["lens"] == "WASTE", "recoverable_cost"].sum()
        if not records.empty else 0.0
    )
    opp_total = (
        records.loc[records["lens"] == "OPPORTUNITY", "recoverable_cost"].sum()
        if not records.empty else 0.0
    )
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

    # The shared coverage table, at this cluster's scope. It used to live here, driven by a
    # hand-maintained rule→group map; it's derived from the rule pool now, so a new
    # WasteRule appears here (and on every other provider's tab) with no edit.
    efficiency_waste.rule_coverage_table(
        _PROVIDER,
        records,
        measured_types,
        key=f"redshift_rule_coverage_{_slug(cluster_id)}",
        scope_note="for this cluster",
    )

    # Reuses efficiency_waste's own lens-table renderer — same WASTE/OPPORTUNITY
    # split, never summed (a cluster can be both, different remedies). Its own
    # sub-$1 floor keeps this table impact-ranked; the coverage table above is where
    # a real-but-unpriced finding (Redshift bills neither per-table nor per-query)
    # stays visible instead of disappearing.
    efficiency_waste.lens_table(
        records, "WASTE", "Waste — tune or right-size it", f"redshift_waste_{_slug(cluster_id)}"
    )
    efficiency_waste.lens_table(
        records, "OPPORTUNITY", "Opportunity — move it to cheaper compute",
        f"redshift_opp_{_slug(cluster_id)}",
    )
