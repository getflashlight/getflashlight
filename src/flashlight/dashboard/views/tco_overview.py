"""TCO overview — Databricks DBU + attributed AWS infra, plus the honest
unattributed bucket. The product's headline view.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
from nicegui import ui

from flashlight.dashboard import chrome
from flashlight.dashboard.data import gold_df
from flashlight.dashboard.theme import compact_money, provider_color

_COMPONENT_LABELS = {
    "dbu_cost": "DBU cost",
    "attributed_infra_cost": "Attributed infra",
    "unattributed_infra_cost": "Unattributed AWS",
}
_COMPONENT_COLORS = {
    "DBU cost": "#3987e5",
    "Attributed infra": "#9085e9",
    "Unattributed AWS": "#c98500",
}

_TCO_FOOTNOTE = (
    "How we calculate TCO: Classic Databricks compute = DBU cost + the AWS EC2/EBS "
    "we can tie to a cluster. Serverless and SQL warehouses = DBU only (no separate infra "
    "line in this view). Unattributed AWS is spend we see in billing but cannot yet map to "
    "a Databricks cluster — it is shown honestly, never hidden."
)


def render() -> None:
    chrome.section_title("Total Cost of Ownership")
    chrome.section_caption(
        "Databricks DBU + the underlying AWS infra it runs on — plus what we can't yet attribute."
    )

    summary = gold_df("SELECT * FROM shared.tco_summary_month ORDER BY charge_month")
    if summary.empty:
        ui.label(
            "No total-cost view yet — this page needs both Databricks and AWS billing "
            "so we can attribute infrastructure spend."
        ).classes("text-sm").style(f"color:{chrome.INK_MUTED}")
        return

    # Latest available month — no cross-page date-range dependency (the breakdown
    # chart below already shows the full history).
    available = summary["charge_month"].astype(str).tolist()
    month_key = max(available)
    row = summary[summary["charge_month"].astype(str) == month_key].iloc[0]
    month_label = pd.Timestamp(month_key).strftime("%b %Y")

    total = row["total_cost"]
    coverage = f"{(1 - row['unattributed_infra_cost'] / total):.0%} attributed" if total else ""
    chrome.section_caption(f"Showing {month_label} (latest available month).")
    chrome.kpi_row(
        [
            (f"Total TCO · {month_label}", compact_money(total), "DBU + infra"),
            (
                "DBU cost",
                compact_money(row["dbu_cost"]),
                "Databricks compute",
                provider_color(group="databricks"),
            ),
            (
                "Unattributed AWS",
                compact_money(row["unattributed_infra_cost"]),
                "not yet mapped",
                "unattributed",
            ),
            ("Attribution coverage", coverage or "—", "of total TCO"),
        ],
    )
    with ui.expansion("How is TCO calculated?").classes("w-full").style(
        f"color:{chrome.INK_SECONDARY}"
    ):
        ui.label(_TCO_FOOTNOTE).classes("text-sm").style(f"color:{chrome.INK_SECONDARY}")

    with chrome.panel():
        _breakdown(summary)

    with chrome.panel():
        with ui.tabs().classes("w-full") as tabs:
            tab_db = ui.tab("Databricks clusters")
            tab_eks = ui.tab("EKS clusters")
        with ui.tab_panels(tabs, value=tab_db).classes("w-full").style("background:transparent;"):
            with ui.tab_panel(tab_db):
                _databricks_clusters(month_key)
            with ui.tab_panel(tab_eks):
                _eks_clusters(month_key)


def _breakdown(summary: pd.DataFrame) -> None:
    melted = summary.melt(
        id_vars="charge_month",
        value_vars=list(_COMPONENT_LABELS),
        var_name="component",
        value_name="cost",
    )
    melted["component"] = melted["component"].map(_COMPONENT_LABELS)
    melted["month"] = pd.to_datetime(melted["charge_month"]).dt.strftime("%Y-%m")
    chrome.panel_title("Monthly TCO — DBU vs attributed vs unattributed")
    fig = px.bar(
        melted,
        x="month",
        y="cost",
        color="component",
        color_discrete_map=_COMPONENT_COLORS,
        category_orders={"component": list(_COMPONENT_COLORS)},
        labels={"month": "", "cost": "", "component": ""},
    )
    fig.update_layout(barmode="stack")
    chrome.plot(chrome.style_fig(fig, has_legend=True, height=320, category_x=True))


def _databricks_clusters(month: str) -> None:
    chrome.panel_title(f"Per-cluster DBU + attributed infra · {month}")
    clusters = gold_df(
        "SELECT cluster_id, compute_class, tco_basis, dbu_cost, infra_cost, tco_cost, "
        f"infra_pct_of_tco FROM shared.tco_by_cluster_month WHERE charge_month = '{month}' "
        "ORDER BY tco_cost DESC"
    )
    if clusters.empty:
        ui.label("No Databricks cluster rows for this month.").classes("text-sm").style(
            f"color:{chrome.INK_MUTED}"
        )
        return
    chrome.searchable_table(
        clusters,
        key="tco_db_clusters",
        search_col="cluster_id",
        money_cols=["dbu_cost", "infra_cost", "tco_cost"],
        pct_cols=["infra_pct_of_tco"],
        rename={
            "cluster_id": "Cluster",
            "compute_class": "Compute class",
            "tco_basis": "TCO basis",
            "dbu_cost": "DBU cost",
            "infra_cost": "Infra cost",
            "tco_cost": "TCO",
            "infra_pct_of_tco": "Infra % of TCO",
        },
    )


def _eks_clusters(month: str) -> None:
    chrome.panel_title(f"Per-cluster control plane + node TCO · {month}")
    eks = gold_df(
        "SELECT cluster_name, control_plane_cost, node_ec2_cost, node_ebs_cost, "
        f"eks_tco, nodes_attributed FROM shared.tco_eks_by_cluster_month "
        f"WHERE charge_month = '{month}' ORDER BY eks_tco DESC"
    )
    if eks.empty:
        ui.label("No EKS cluster rows for this month.").classes("text-sm").style(
            f"color:{chrome.INK_MUTED}"
        )
        return
    chrome.searchable_table(
        eks,
        key="tco_eks_clusters",
        search_col="cluster_name",
        money_cols=["control_plane_cost", "node_ec2_cost", "node_ebs_cost", "eks_tco"],
        int_cols=["nodes_attributed"],
        rename={
            "cluster_name": "Cluster",
            "control_plane_cost": "Control plane",
            "node_ec2_cost": "Node EC2",
            "node_ebs_cost": "Node EBS",
            "eks_tco": "EKS TCO",
            "nodes_attributed": "Nodes",
        },
    )
