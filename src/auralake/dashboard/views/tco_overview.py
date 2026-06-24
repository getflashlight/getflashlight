"""TCO overview — Databricks DBU + attributed AWS infra, plus the honest
unattributed bucket. The product's headline view.

Layout mirrors the original Grafana ``tco_overview`` board (recovered from git
history): TCO stats, a by-service-category donut, the monthly DBU/infra
breakdown, a daily provider trend, and the per-cluster TCO tables.
"""

from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data
from auralake.dashboard.theme import (
    PALETTE,
    kpi_cards,
    month_filter,
    plotly,
    shadcn_table,
    style_fig,
    tabs,
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

    summary = gold_df("SELECT * FROM gold.tco_summary_month ORDER BY charge_month")
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
            (f"Total TCO · {month}", f"${total:,.0f}", "DBU + infra"),
            ("DBU cost", f"${row['dbu_cost']:,.0f}", "Databricks compute"),
            ("Unattributed AWS", f"${row['unattributed_infra_cost']:,.0f}", "not yet mapped"),
            ("Attribution coverage", coverage or "—", "of total TCO"),
        ],
        key="tco",
    )

    st.write("")
    active = tabs(["Breakdown", "Databricks clusters", "EKS clusters"], key="tco_tabs")

    if active == "Breakdown":
        _breakdown(summary)
    elif active == "Databricks clusters":
        _databricks_clusters(month)
    elif active == "EKS clusters":
        _eks_clusters(month)


def _breakdown(summary: pd.DataFrame) -> None:
    left, right = st.columns(2)
    with left:
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
    with right:
        cats = gold_df(
            "SELECT service_category, SUM(net_cost) AS net_cost FROM gold.spend_by_service_month "
            "GROUP BY service_category ORDER BY net_cost DESC"
        )
        if cats.empty:
            st.info("No service-category rows.")
        else:
            fig = px.pie(
                cats,
                names="service_category",
                values="net_cost",
                hole=0.55,
                color_discrete_sequence=PALETTE,
            )
            fig.update_traces(textposition="inside", textinfo="percent")
            plotly(
                style_fig(fig, currency_axis=None),
                title="Spend by service category (all time)",
                key="tco_pie",
            )

    daily = gold_df(
        "SELECT charge_day, provider_name, net_cost FROM gold.spend_trend_daily ORDER BY charge_day"
    )
    if not daily.empty:
        plotly(
            style_fig(
                px.line(
                    daily,
                    x="charge_day",
                    y="net_cost",
                    color="provider_name",
                    labels={"charge_day": "", "net_cost": "Net cost", "provider_name": ""},
                )
            ),
            title="Daily spend trend by provider",
            key="tco_daily",
        )


def _databricks_clusters(month: str) -> None:
    st.caption(f"Latest charge month · {month}")
    clusters = gold_df(
        "SELECT cluster_id, compute_class, tco_basis, dbu_cost, infra_cost, tco_cost, "
        f"infra_pct_of_tco FROM gold.tco_by_cluster_month WHERE charge_month = '{month}' "
        "ORDER BY tco_cost DESC"
    )
    if clusters.empty:
        st.info("No Databricks cluster rows for this month.")
        return
    shadcn_table(
        clusters,
        key="tco_clusters",
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
    st.caption(f"Latest charge month · {month}")
    eks = gold_df(
        "SELECT cluster_name, control_plane_cost, node_ec2_cost, node_ebs_cost, "
        f"eks_tco, nodes_attributed FROM gold.tco_eks_by_cluster_month "
        f"WHERE charge_month = '{month}' ORDER BY eks_tco DESC"
    )
    if eks.empty:
        st.info("No EKS cluster rows for this month.")
        return
    shadcn_table(
        eks,
        key="tco_eks",
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
