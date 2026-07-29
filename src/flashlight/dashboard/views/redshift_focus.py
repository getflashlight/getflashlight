"""Redshift-scoped view — the AWS page's own render, kept look-and-feel
consistent with ``provider_focus.render()`` (Databricks/other providers): same
page shell (title + date range control + persistent KPI row + one-line
summary, all above the tabs so they don't disappear when you switch tabs), same
tab set (Trend & changes / Breakdown / Tags / Optimization), and the same
one-``chrome.panel()``-per-tab card convention. Not its own GOLD provider
group — Redshift's cost already flows into ``aws.*`` via the ``aws_focus``
connector (AWS Data Exports FOCUS carries Redshift's own SKUs); its own
connector only supplies efficiency/waste telemetry. So this page:

- **Trend & changes** / **Breakdown** / **Tags**: filter the ``aws`` group's
  views to Redshift's own FOCUS ``service_name`` values
  (:data:`REDSHIFT_SERVICE_NAMES`) — there's no ``aws.monthly_bill`` or
  ``aws.spend_trend_daily`` shortcut since those views carry no
  ``service_name`` dimension, so totals (and the date-range bounds) are built
  from ``spend_by_service_month`` instead (monthly grain only — no daily trend
  at this scope). Tags similarly has no direct service filter, so it scopes
  ``spend_by_sku_tag_month`` to the ``sku_id`` set Redshift's own resource rows
  carry.
- **Optimization**: faceted per cluster (:func:`_waste_section`) — one section per
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
  (just under a different ``provider_name``). :func:`_rule_coverage_table` also
  cross-references ``efficiency.efficiency_entity_month`` (every rule this connector
  evaluates, not just the ones that fired) so a rule that found nothing reads
  differently from one whose telemetry never arrived this window.
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
import plotly.express as px
from nicegui import ui

from flashlight.dashboard import chrome, router
from flashlight.dashboard.chrome import DateState
from flashlight.dashboard.data import gold_df
from flashlight.dashboard.data import to_date as _d
from flashlight.dashboard.theme import compact_money, md_money
from flashlight.dashboard.views.efficiency_waste import _lens_table
from flashlight.dashboard.views.provider_focus import _commitment, _cost_subcategory
from flashlight.efficiency.waste_rules import WASTE_RULES
from flashlight.ingest._redshift_service_names import REDSHIFT_SERVICE_NAMES

_GROUP = "aws"
_PROVIDER = "AWS"
_CONNECTOR = "redshift"
_SERVICE_IN = ", ".join(f"'{s}'" for s in sorted(REDSHIFT_SERVICE_NAMES))
_SKU_SCOPE = (
    f"sku_id IN (SELECT DISTINCT sku_id FROM {_GROUP}.resource_month "
    f"WHERE service_name IN ({_SERVICE_IN}))"
)

# Every WasteRule this connector can fire, grouped by the entity_type it evaluates —
# the rule-coverage table's own structure. Pulled by category from WASTE_RULES (not
# re-stated here) so label/lens text can't drift from the one source of truth.
_RULE_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Cluster", "sql_warehouse", (
        "idle",
        "redshift_concurrency_scaling_overage",
        "redshift_ri_coverage_gap",
        "redshift_spectrum_scan_cost",
        "redshift_disk_spill_queries",
        "redshift_wlm_queue_wait",
    )),
    ("Per-user", "sql_warehouse_user", (
        "sql_warehouse_user_concentration",
    )),
    ("Per-query-pattern", "query_pattern", (
        "redshift_query_pattern_high_spill",
        "redshift_query_pattern_skew",
    )),
    ("Per-table", "table", (
        "redshift_stale_compression_encoding",
        "redshift_table_maintenance_stale",
        "redshift_table_unused",
        "redshift_spectrum_table_scan",
    )),
)
_RULE_INDEX = {r.category: r for r in WASTE_RULES}


def _sql_str(value: str) -> str:
    return value.replace("'", "''")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", value)


def render() -> None:
    scope = gold_df(
        f"SELECT min(charge_month) AS lo, max(charge_month) AS hi "
        f"FROM {_GROUP}.spend_by_service_month WHERE service_name IN ({_SERVICE_IN})"
    )
    if scope.empty or pd.isna(scope["lo"].iloc[0]):
        ui.label("No Redshift spend found in the AWS bill.").classes("text-sm").style(
            f"color:{chrome.INK_MUTED}"
        )
        return

    lo = _d(scope["lo"].iloc[0]).replace(day=1)
    # No Redshift-only daily view to bound "today" against, so borrow the whole AWS
    # bill's latest billed day — Redshift can't have billed data later than the
    # account's own most recent day.
    bill_hi = gold_df(f"SELECT max(charge_day) AS hi FROM {_GROUP}.spend_trend_daily")
    hi = (
        _d(bill_hi["hi"].iloc[0])
        if not bill_hi.empty and not pd.isna(bill_hi["hi"].iloc[0])
        else _d(scope["hi"].iloc[0])
    )
    date_state: DateState = {
        "start": max(lo, chrome.months_back(hi, 6)),
        "end": hi,
        "bounds_min": lo,
        "bounds_max": hi,
    }

    with ui.row().classes("items-center justify-between w-full"):
        chrome.section_title("Redshift spend")
        chrome.date_range_control(date_state, lambda: body.refresh())

    @ui.refreshable
    def body() -> None:
        start, end = date_state["start"], date_state["end"]
        sm = start.replace(day=1)
        partial = router.range_has_partial_month(end)
        cap = (
            "Scoped from the AWS bill to Redshift's own FOCUS service names: "
            f"{', '.join(sorted(REDSHIFT_SERVICE_NAMES))}."
        )
        if partial:
            cap += " Partial month — current month is still accruing."
        chrome.section_caption(cap)

        monthly = gold_df(
            f"SELECT charge_month, sum(net_cost) AS net_cost "
            f"FROM {_GROUP}.spend_by_service_month "
            f"WHERE service_name IN ({_SERVICE_IN}) "
            f"AND charge_month >= '{sm}' AND charge_month <= '{end}' "
            "GROUP BY charge_month ORDER BY charge_month"
        )
        if monthly.empty:
            ui.label("No Redshift spend in the selected range.").classes("text-sm").style(
                f"color:{chrome.INK_MUTED}"
            )
            return

        _kpis(monthly, start, end, partial=partial)
        ui.markdown(_summary_line(monthly)).style(
            f"color:{chrome.INK_SECONDARY};font-size:13px;"
        )

        with ui.tabs().classes("w-full") as tabs:
            tab_trend = ui.tab("Trend & changes")
            tab_breakdown = ui.tab("Breakdown")
            tab_tags = ui.tab("Tags")
            tab_optimization = ui.tab("Optimization")
        with ui.tab_panels(tabs, value=tab_trend).classes("w-full").style(
            "background:transparent;"
        ):
            with ui.tab_panel(tab_trend), chrome.panel():
                _trend_section(monthly)
            with ui.tab_panel(tab_breakdown), chrome.panel():
                _breakdown_section(sm, end)
            with ui.tab_panel(tab_tags), chrome.panel():
                _tags_section(sm, end)
            # Optimization draws its own section titles/panels internally (same
            # convention as Databricks' extra_tabs, e.g. Efficiency & Waste) — no
            # shared chrome.panel() wrapper here.
            with ui.tab_panel(tab_optimization):
                _waste_section()

    body()


def _kpis(monthly: pd.DataFrame, start: date, end: date, *, partial: bool) -> None:
    net = float(monthly["net_cost"].sum())
    latest = monthly.iloc[-1]
    latest_month = pd.Timestamp(latest["charge_month"])
    span = f"{start:%b %d} → {end:%b %d}" + (" · partial" if partial else "")
    chrome.kpi_row(
        [
            ("Redshift net", compact_money(net), span),
            (
                f"Latest · {latest_month:%b %Y}",
                compact_money(float(latest["net_cost"])),
                "net cost",
            ),
        ],
        columns=2,
    )


def _summary_line(monthly: pd.DataFrame) -> str:
    """One-line NL summary, same spirit as ``summary.provider_spend_summary`` but
    computed off the ``monthly`` frame the caller already fetched (Redshift has no
    ``monthly_bill`` shortcut to re-query — see the module docstring).
    """
    net = float(monthly["net_cost"].sum())
    if len(monthly) < 2:
        return f"Redshift net spend is {md_money(net)} in the selected window."

    cur = float(monthly.iloc[-1]["net_cost"])
    prev = float(monthly.iloc[-2]["net_cost"])
    if not prev:
        return f"Redshift net spend is {md_money(net)} in the selected window."

    prior_month = pd.Timestamp(monthly.iloc[-2]["charge_month"])
    delta = cur - prev
    if delta == 0:
        return (
            f"Redshift net spend {md_money(net)} in the selected window. "
            f"Flat vs {prior_month:%b %Y}."
        )
    verb = "rose" if delta > 0 else "fell"
    return (
        f"Redshift net spend {md_money(net)} in the selected window. Latest month {verb} "
        f"{md_money(abs(delta))} ({100 * delta / prev:+.1f}%) vs {prior_month:%b %Y}."
    )


def _trend_section(monthly: pd.DataFrame) -> None:
    chrome.panel_title("Monthly Redshift spend")
    chart_df = monthly.copy()
    chart_df["month"] = pd.to_datetime(chart_df["charge_month"]).dt.strftime("%Y-%m")
    fig = px.bar(chart_df, x="month", y="net_cost", labels={"month": "", "net_cost": ""})
    fig.update_traces(marker_color=chrome.ACCENT)
    chrome.plot(chrome.style_fig(fig, has_legend=False, category_x=True))


def _breakdown_section(sm: date, end: date) -> None:
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
        chrome.panel_title("Spend by SKU")
        chrome.flat_table(
            sku, key="redshift_sku", money_cols=["net_cost"],
            rename={"sku_id": "SKU", "description": "Description", "net_cost": "Net cost"},
        )

    # The only current x_cost_subcategory producer — safe to call unfiltered by
    # service, since no other AWS service populates this view (see the function's
    # own docstring). Draws its own panel_title, so no extra wrapping needed here.
    _cost_subcategory(_GROUP, end, sm)
    # Account-wide, NOT Redshift-scoped on purpose: a Savings Plan/RI commitment
    # isn't tied to one service, so this is the whole AWS account's commitment
    # coverage (Redshift RIs plus EC2/RDS/etc Savings Plans), same as the
    # account-level "committed" bucket above. Renders nothing if empty.
    _commitment(_GROUP, end, sm)


def _tags_section(sm: date, end: date) -> None:
    """Spend-by-tag, scoped to Redshift like the rest of this page. There's no
    per-service tag view, so this scopes ``spend_by_sku_tag_month`` (which carries
    ``sku_id`` but not ``service_name``) down to the SKU ids Redshift's own
    resource rows use — same trick as ``_invoice_scope`` above, one level up.
    """
    chrome.panel_title("Spend by tag")
    _in_range = f"charge_month >= '{sm}' AND charge_month <= '{end}'"
    keys = gold_df(
        f"SELECT tag_key, sum(net_cost) AS net FROM {_GROUP}.spend_by_sku_tag_month "
        f"WHERE {_SKU_SCOPE} AND {_in_range} GROUP BY tag_key ORDER BY net DESC"
    )
    if keys.empty:
        ui.label("No tagged Redshift spend in range.").classes("text-sm").style(
            f"color:{chrome.INK_MUTED}"
        )
        return

    options = keys["tag_key"].tolist()
    default = "team" if "team" in options else options[0]
    body_container = ui.column().classes("w-full gap-4")

    @ui.refreshable
    def _tag_values(sel: str) -> None:
        body_container.clear()
        with body_container:
            tags = gold_df(
                f"SELECT tag_value, sum(net_cost) AS net_cost "
                f"FROM {_GROUP}.spend_by_sku_tag_month "
                f"WHERE tag_key = '{_sql_str(sel)}' AND {_SKU_SCOPE} AND {_in_range} "
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
    identifier itself (see ingest/connectors/redshift.py).
    """
    df = gold_df(
        "SELECT DISTINCT entity_id FROM efficiency.efficiency_entity_month "
        f"WHERE provider_name = '{_PROVIDER}' AND x_source_connector = '{_CONNECTOR}' "
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
        f"WHERE provider_name = '{_PROVIDER}' AND x_source_connector = '{_CONNECTOR}' "
        f"AND {scope}"
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

    _rule_coverage_table(cluster_id, records, measured_types)

    # Reuses efficiency_waste's own lens-table renderer — same WASTE/OPPORTUNITY
    # split, never summed (a cluster can be both, different remedies). Its own
    # sub-$1 floor keeps this table impact-ranked; the coverage table above is where
    # a real-but-unpriced finding (Redshift bills neither per-table nor per-query)
    # stays visible instead of disappearing.
    _lens_table(
        records, "WASTE", "Waste — tune or right-size it", f"redshift_waste_{_slug(cluster_id)}"
    )
    _lens_table(
        records, "OPPORTUNITY", "Opportunity — move it to cheaper compute",
        f"redshift_opp_{_slug(cluster_id)}",
    )


def _rule_coverage_rows(records: pd.DataFrame, measured_types: set[str]) -> list[dict[str, object]]:
    """Pure computation behind the rule-coverage table — every rule this connector can
    fire for this cluster, each resolved to fired (priced or unpriced) / clean / no
    data. Split out from rendering so the fired-vs-clean-vs-no-data logic is directly
    testable without a NiceGUI context.
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
    for group_label, entity_type, categories in _RULE_GROUPS:
        for category in categories:
            rule = _RULE_INDEX[category]
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
            elif entity_type in measured_types:
                recoverable, priced, status = 0.0, False, "clean"
            else:
                recoverable, priced, status = 0.0, False, "no data"
            rows.append(
                {
                    # Not rendered (dropped before flat_table) — a stable key for
                    # callers/tests to look a specific rule's row up by, instead of
                    # matching on its prose label.
                    "category": category,
                    "Group": group_label,
                    "Rule": rule.label,
                    "Lens": rule.lens,
                    "Status": status,
                    "Recoverable": recoverable if priced else float("nan"),
                }
            )
    return rows


def _status_dot_style(status: str, lens: str) -> tuple[str, str]:
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


def _rule_coverage_table(
    cluster_id: str, records: pd.DataFrame, measured_types: set[str]
) -> None:
    rows = _rule_coverage_rows(records, measured_types)
    with chrome.panel():
        chrome.panel_title("Optimization rule coverage")
        chrome.section_caption(
            "Every rule this connector evaluates for this cluster — not only the ones "
            "with a dollar figure. “unpriced” = a real, confirmed finding "
            "Redshift can't honestly price (it doesn't bill per-table or per-query). "
            "“no data” = the telemetry pull came back empty this window — "
            "not the same as clean."
        )
        table = chrome.flat_table(
            pd.DataFrame(rows).drop(columns="category"),
            key=f"redshift_rule_coverage_{_slug(cluster_id)}",
            money_cols=["Recoverable"],
        )
        for row, r in zip(table.rows, rows, strict=True):
            dot_style, color = _status_dot_style(str(r["Status"]), str(r["Lens"]))
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
