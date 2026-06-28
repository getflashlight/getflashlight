"""TCO overview — Databricks DBU + attributed AWS infra, plus the honest
unattributed bucket. The product's headline view.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.context import global_range, tco_charge_month
from auralake.dashboard.data import NO_DATA_MSG, gold_df, has_data
from auralake.dashboard.theme import (
    compact_money,
    filterable_table,
    kpi_cards,
    panel,
    plotly,
    provider_color,
    section_caption,
    section_title,
    style_fig,
)

_COMPONENT_LABELS = {
    "dbu_cost": "DBU cost",
    "attributed_infra_cost": "Attributed infra",
    "unattributed_infra_cost": "Unattributed AWS",
}
_COMPONENT_COLORS = {
    "DBU cost": "#0E7C86",
    "Attributed infra": "#2E86AB",
    "Unattributed AWS": "#C28B4B",
}

_TCO_FOOTNOTE = (
    "**How we calculate TCO:** Classic Databricks compute = DBU cost + the AWS EC2/EBS "
    "we can tie to a cluster. Serverless and SQL warehouses = DBU only (no separate infra "
    "line in this view). Unattributed AWS is spend we see in billing but cannot yet map to "
    "a Databricks cluster — it is shown honestly, never hidden."
)


def render() -> None:
    st.title("Total Cost of Ownership")
    st.caption(
        "Databricks DBU + the underlying AWS infra it runs on — plus what we can't yet attribute."
    )
    if not has_data():
        st.info(NO_DATA_MSG)
        return

    summary = gold_df("SELECT * FROM shared.tco_summary_month ORDER BY charge_month")
    if summary.empty:
        st.info(
            "No total-cost view yet — this page needs both Databricks and AWS billing "
            "so we can attribute infrastructure spend."
        )
        return

    _, end = global_range()
    month_key = tco_charge_month(end)
    available = summary["charge_month"].astype(str).tolist()
    if month_key not in available:
        month_key = max(available)
    row = summary[summary["charge_month"].astype(str) == month_key].iloc[0]
    month_label = pd.Timestamp(month_key).strftime("%b %Y")

    total = row["total_cost"]
    coverage = f"{(1 - row['unattributed_infra_cost'] / total):.0%} attributed" if total else ""
    section_caption(
        f"Showing **{month_label}** (from sidebar date range ending {end:%b %d, %Y})."
    )
    kpi_cards(
        [
            (f"Total TCO · {month_label}", compact_money(total), "DBU + infra", "default"),
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
            ("Attribution coverage", coverage or "—", "of total TCO", "default"),
        ],
        key="tco",
        accent=provider_color(group="databricks"),
    )
    with st.expander("How is TCO calculated?"):
        st.markdown(_TCO_FOOTNOTE)

    with panel(tone="teal", flush=True):
        _breakdown(summary)
    with panel(tone="default"):
        tab_db, tab_eks = st.tabs(["Databricks clusters", "EKS clusters"])
        with tab_db:
            _databricks_clusters(month_key)
        with tab_eks:
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
    plotly(
        style_fig(
            px.bar(
                melted,
                x="month",
                y="cost",
                color="component",
                color_discrete_map=_COMPONENT_COLORS,
                category_orders={"component": list(_COMPONENT_COLORS)},
                labels={"month": "", "cost": "Cost", "component": ""},
            ),
            has_legend=True,
        ),
        title="Monthly TCO — DBU vs attributed vs unattributed",
        title_flush=True,
        key="tco_breakdown",
    )


def _databricks_clusters(month: str) -> None:
    section_title("Databricks clusters", flush=True)
    section_caption(f"Per-cluster DBU + attributed infra · {month}")
    clusters = gold_df(
        "SELECT cluster_id, compute_class, tco_basis, dbu_cost, infra_cost, tco_cost, "
        f"infra_pct_of_tco FROM shared.tco_by_cluster_month WHERE charge_month = '{month}' "
        "ORDER BY tco_cost DESC"
    )
    if clusters.empty:
        st.info("No Databricks cluster rows for this month.")
        return
    filterable_table(
        clusters,
        filter_col="cluster_id",
        file_name="databricks_clusters.csv",
        key="tco_db_clusters",
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
    section_title("EKS clusters", flush=True)
    section_caption(f"Per-cluster control plane + node TCO · {month}")
    eks = gold_df(
        "SELECT cluster_name, control_plane_cost, node_ec2_cost, node_ebs_cost, "
        f"eks_tco, nodes_attributed FROM shared.tco_eks_by_cluster_month "
        f"WHERE charge_month = '{month}' ORDER BY eks_tco DESC"
    )
    if eks.empty:
        st.info("No EKS cluster rows for this month.")
        return
    filterable_table(
        eks,
        filter_col="cluster_name",
        file_name="eks_clusters.csv",
        key="tco_eks_clusters",
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
