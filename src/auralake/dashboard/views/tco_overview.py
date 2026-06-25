"""TCO overview — Databricks DBU + attributed AWS infra, plus the honest
unattributed bucket. The product's headline view.

Reads the cross-provider ``shared.*`` TCO group only — per-provider spend lives on
each provider's own page. Layout: TCO stats, the monthly DBU/infra breakdown, then
the per-cluster Databricks + EKS TCO tables.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data
from auralake.dashboard.theme import (
    compact_money,
    html_table,
    kpi_cards,
    month_filter,
    plotly,
    style_fig,
)

# Stable component → label/colour mapping so the stacked bars read the same every month.
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


def render() -> None:
    st.title("Total Cost of Ownership")
    st.caption(
        "Databricks DBU + the underlying AWS infra it runs on — plus what we can't yet attribute."
    )
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    summary = gold_df("SELECT * FROM shared.tco_summary_month ORDER BY charge_month")
    if summary.empty:
        st.info("No TCO rows — needs Databricks + AWS data to attribute.")
        return

    months = summary["charge_month"].astype(str).tolist()
    month = month_filter(months, key="tco_month") or max(months)
    row = summary[summary["charge_month"].astype(str) == month].iloc[0]

    total = row["total_cost"]
    coverage = f"{(1 - row['unattributed_infra_cost'] / total):.0%} attributed" if total else ""
    kpi_cards(
        [
            (f"Total TCO · {month}", compact_money(total), "DBU + infra"),
            ("DBU cost", compact_money(row["dbu_cost"]), "Databricks compute"),
            ("Unattributed AWS", compact_money(row["unattributed_infra_cost"]), "not yet mapped"),
            ("Attribution coverage", coverage or "—", "of total TCO"),
        ],
        key="tco",
    )

    st.divider()
    _breakdown(summary)
    st.write("")
    _databricks_clusters(month)
    st.write("")
    _eks_clusters(month)


def _breakdown(summary: pd.DataFrame) -> None:
    # The monthly DBU vs attributed vs unattributed stack, straight from the shared
    # TCO summary. Per-provider spend lives on each provider's own page now, so this
    # page stays focused on the cross-provider TCO numbers.
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
            )
        ),
        title="Monthly TCO — DBU vs attributed vs unattributed",
        key="tco_breakdown",
    )


def _databricks_clusters(month: str) -> None:
    st.markdown("##### Databricks clusters")
    st.caption(f"Per-cluster DBU + attributed infra · {month}")
    clusters = gold_df(
        "SELECT cluster_id, compute_class, tco_basis, dbu_cost, infra_cost, tco_cost, "
        f"infra_pct_of_tco FROM shared.tco_by_cluster_month WHERE charge_month = '{month}' "
        "ORDER BY tco_cost DESC"
    )
    if clusters.empty:
        st.info("No Databricks cluster rows for this month.")
        return
    html_table(
        clusters,
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
    st.markdown("##### EKS clusters")
    st.caption(f"Per-cluster control plane + node TCO · {month}")
    eks = gold_df(
        "SELECT cluster_name, control_plane_cost, node_ec2_cost, node_ebs_cost, "
        f"eks_tco, nodes_attributed FROM shared.tco_eks_by_cluster_month "
        f"WHERE charge_month = '{month}' ORDER BY eks_tco DESC"
    )
    if eks.empty:
        st.info("No EKS cluster rows for this month.")
        return
    html_table(
        eks,
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
