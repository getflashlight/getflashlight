"""Backing compute — the AWS-billed EC2 cost of a Databricks cluster's cloud VMs.

Databricks' own bill (``system.billing.usage``) covers DBU compute only; for a CLASSIC
(non-serverless) cluster, Databricks orchestrates the creation of the underlying cloud VM
on the customer's own AWS account, and that VM is billed separately, by AWS, as EC2. This
tab reports the EC2 cost of the instances Databricks' own ``system.compute.node_timeline``
says backed a cluster — the identical shape as Backing storage, for compute instead of
storage.

**The rule this whole module exists to hold**: these dollars are billed by AWS and are
already counted in ``aws.monthly_bill``. They are never added to Databricks spend — that
sum is the TCO join CLAUDE.md removed, and nothing here recreates it. Two bills, reported
side by side.

**Classic compute only, so this is a FLOOR.** ``node_timeline`` has zero rows for
serverless SQL warehouses, serverless jobs and DLT serverless pipelines — there is no
customer-visible instance for Databricks to report — so a cluster's absence from this tab
is never evidence it carried no cloud-infra cost, and the gap grows as serverless adoption
grows. See ``066_gold_compute.sql``'s header and ``docs/design/backing-compute.md``.

Two more honesty constraints shape the panels:

* The map is bounded by ``node_timeline``'s ~90-day retention: an instance's history from
  before that window, at the time it was ingested, can never be recovered. Unlike backing
  storage's present-tense snapshot applied to all history, this join is genuinely per
  (instance, charge_month) — matched against the exact month the instance actually ran in.
* Absence of a mapping is not absence of Databricks compute cost. Every empty state names
  its own cause (no map / no EC2 rows / map but nothing managed) rather than going blank.

Deliberately **not** here: a per-instance list of everything unmapped (EBS volumes, other
EC2 workloads in the account). Not this tab's subject; per-row detail stays queryable in
GOLD/MCP via ``compute.backing_compute_month``.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.express as px
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df, gold_view_published
from flashlight.dashboard.theme import compact_money

_GROUP = "compute"
_VIEW = "backing_compute_month"

#: Rendered when there is no compute-instance map at all. Says outright what absence does
#: NOT mean.
NO_MAP_MSG = (
    "No compute-instance map yet — run `flashlight ingest` with a Databricks connection "
    "configured. Absence of a mapping is not absence of Databricks compute cost: it means "
    "nothing has looked yet."
)

#: Map present, but no EC2 rows in the AWS bill to label.
NO_EC2_COST_MSG = (
    "No Amazon EC2 rows in the AWS bill for this lake. Add `Amazon Elastic Compute Cloud` "
    "to this connection's `include_services` in connections.yml (it is in the default), "
    "then re-run `flashlight ingest`."
)

#: EC2 cost present, but nothing managed — the case most likely to be misread as
#: "Databricks has no compute cost".
NO_CLUSTERS_MSG = (
    "Found EC2 spend but no Databricks-managed compute instances. Either the "
    "compute-instance pull has not run, the cluster ran on serverless compute (which has "
    "no customer-visible instance to report), or its activity predates "
    "system.compute.node_timeline's ~90-day retention. This does not mean Databricks has "
    "no compute cost."
)

#: FOCUS's own pricing-model vocabulary (PricingCategory) — Dynamic IS Spot on AWS,
#: there is no separate "spot" value in FOCUS. '(unknown)' is 066_gold_compute.sql's
#: fallback for a row whose AWS export carried no value at all (older exports, or a
#: charge FOCUS itself allows to be null) — kept as its own labelled bucket rather than
#: folded into "Standard", which would overstate on-demand share.
_PRICING_LABELS = {
    "Standard": "On-demand / negotiated",
    "Dynamic": "Spot",
    "Committed": "Reserved / Savings Plan",
    "Other": "Other pricing model",
    "(unknown)": "Unknown",
}


def _df(sql: str) -> pd.DataFrame:
    """Query GOLD, returning empty on any issue (the view may be unbuilt)."""
    try:
        return gold_df(sql)
    except Exception:  # noqa: BLE001 - missing/empty view → render an empty state
        return pd.DataFrame()


def kpi_card(sm: date, end: date) -> chrome.KpiCard | None:
    """This window's Databricks-managed EC2 cost, as a card on the Databricks KPI row.

    **Beside the Databricks bill, never inside it.** Same reasoning as
    ``backing_storage.kpi_card``: this must never become a term in the ``Net Spend``
    total, which stays purely DBU compute (the AWS group's ``monthly_bill`` is where
    these dollars are actually billed, and they are counted there exactly once). Same
    hue as Backing storage's card — both mean the identical thing to a reader: a
    separate, AWS-billed satellite cost, not a slice of net.

    Returns ``None`` — no card at all — when there is nothing to report. A "$0" card
    would say "Databricks compute is free", which is the single misreading this module
    exists to prevent: an unmapped or unpulled cluster is unmeasured, not zero. The
    tab's own empty states name which of those it is.
    """
    if not gold_view_published(_GROUP, _VIEW):
        return None
    df = _df(
        f"SELECT sum(net_cost) AS c FROM {_GROUP}.{_VIEW} WHERE mapping = 'databricks' "
        f"AND charge_month >= '{sm}' AND charge_month <= '{end}'"
    )
    if df.empty or pd.isna(df["c"].iloc[0]) or not float(df["c"].iloc[0]):
        return None
    return ("Databricks Compute", compact_money(float(df["c"].iloc[0])), "", "volume")


def render(sm: date | None = None, end: date | None = None) -> None:
    """Databricks-managed EC2 cost for the page date range.

    ``sm``/``end`` come from ``provider_focus``'s after_breakdown hook so this tab
    shares the page date control. Omitted only for direct calls (tests).
    """
    chrome.section_title("Databricks Compute (billed by AWS)")

    if not gold_view_published(_GROUP, _VIEW):
        ui.label(NO_MAP_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    where = ""
    if sm is not None and end is not None:
        where = f" WHERE charge_month >= '{sm}' AND charge_month <= '{end}'"
    rows = _df(
        "SELECT instance_id, mapping, cluster_id, cluster_name, owner_user, instance_role, "
        f"node_type, region_id, pricing_category, charge_month, net_cost "
        f"FROM {_GROUP}.{_VIEW}{where}"
    )
    if rows.empty:
        ui.label(NO_EC2_COST_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    mapped = rows[rows["mapping"] == "databricks"]
    if mapped.empty:
        # No unmapped instance list here on purpose: with nothing managed, a table of
        # every unrelated EC2 instance is what makes this read as "all your EC2 spend".
        # The denominator is still stated, as one line.
        ui.label(NO_CLUSTERS_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        _denominator_caption(rows)
        return

    _managed_clusters(mapped)
    _pricing_trend(mapped)


def _managed_clusters(mapped: pd.DataFrame) -> None:
    """One row per Databricks cluster, with its name/owner and mapped EC2 cost.

    Cluster-grained, not instance-grained: a cluster's cost is the sum of however many
    driver/worker instances backed it in the window, which is what a reader actually
    wants ("what did this cluster's cloud infra cost?"), not a row per ephemeral
    instance id. cluster_name/owner_user already fall back to the bare cluster_id/
    '(unknown)' at the GOLD layer (066_gold_compute.sql) when system.compute.clusters
    has no row for a cluster, so this table never shows a blank.
    """
    per_cluster = (
        mapped.groupby(["cluster_id", "cluster_name", "owner_user"], as_index=False)
        .agg(net_cost=("net_cost", "sum"), instance_count=("instance_id", "nunique"))
        .sort_values("net_cost", ascending=False)
    )
    if per_cluster.empty:
        return
    per_cluster["net_cost"] = per_cluster["net_cost"].astype(float)
    per_cluster["EC2 cost (AWS-billed)"] = per_cluster["net_cost"].map(
        lambda c: f"${c:,.0f}"
    )
    chrome.panel_title("Databricks-managed compute")
    chrome.searchable_table(
        per_cluster[
            ["cluster_name", "owner_user", "instance_count", "EC2 cost (AWS-billed)"]
        ],
        key="backing_compute_managed",
        search_col="cluster_name",
        rename={
            "cluster_name": "Cluster",
            "owner_user": "Owner",
            "instance_count": "EC2 instances",
        },
    )


def _denominator_caption(rows: pd.DataFrame) -> None:
    """The total EC2 bill this tab is a subset of — kept even when nothing is managed, so
    the empty state still says how much EC2 spend exists and how many instances carry it.
    """
    total = float(rows["net_cost"].sum())
    instances = _real_instances(rows)
    chrome.section_caption(
        f"For scale: {compact_money(total)} of Amazon EC2 spend across {len(instances):,} "
        "instance(s) in this window, none of it currently identified as "
        "Databricks-managed. Per-instance detail is available via MCP "
        "(compute.backing_compute_month)."
    )


def _pricing_trend(mapped: pd.DataFrame) -> None:
    """Monthly cost split by pricing model (Spot / on-demand / committed / other).

    FOCUS's own PricingCategory column, carried straight from the AWS bill — not
    something Databricks' own metadata can tell us (system.compute.node_timeline has
    no per-instance Spot/on-demand signal; a cluster's own "availability" attribute is
    the *policy* it's configured with, not a per-instance, billed-fact result). This is
    the more useful split than driver/worker role: it answers "how much of this is
    already the cheapest pricing available" rather than "how many nodes were leaders".

    A stacked chart with no legend is a chart of unexplained colours, so ``has_legend``
    is on. ``category_x`` is on for the same reason every other monthly bar chart here
    sets it: a "YYYY-MM" string keeps Plotly from auto-detecting a date axis and placing
    its ticks between the bars.
    """
    by_month = (
        mapped.groupby(["charge_month", "pricing_category"], as_index=False)["net_cost"]
        .sum()
        .sort_values("charge_month")
    )
    if by_month.empty:
        return
    by_month["net_cost"] = by_month["net_cost"].astype(float)
    by_month["month"] = pd.to_datetime(by_month["charge_month"]).dt.strftime("%Y-%m")
    by_month["Pricing"] = by_month["pricing_category"].map(
        lambda p: _PRICING_LABELS.get(str(p), str(p))
    )
    chrome.panel_title("Databricks-managed EC2 cost by pricing model")
    fig = px.bar(
        by_month,
        x="month",
        y="net_cost",
        color="Pricing",
        color_discrete_sequence=list(chrome.CATEGORICAL_SLOTS),
        barmode="stack",
        labels={"month": "", "net_cost": "", "Pricing": ""},
    )
    chrome.plot(chrome.style_fig(fig, currency_axis="y", has_legend=True, category_x=True))


def _real_instances(df: pd.DataFrame) -> set[str]:
    """Distinct instance ids, excluding the ``(no resource id)`` placeholder — it is a
    cost bucket in the view, not a real EC2 instance, and counting it would inflate the
    denominator with something that can never be mapped."""
    return {str(i) for i in df["instance_id"].unique() if str(i) != "(no resource id)"}
