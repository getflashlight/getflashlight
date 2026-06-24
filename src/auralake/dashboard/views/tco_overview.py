"""TCO overview — Databricks DBU + attributed AWS infra, plus the honest
unattributed bucket. The product's headline view."""

from __future__ import annotations

import plotly.express as px
import streamlit as st

from auralake.dashboard.data import gold_df, has_data


def render() -> None:
    st.title("Total Cost of Ownership")
    if not has_data():
        st.info("No data yet — run `auralake ingest` to load billing.")
        return

    summary = gold_df("SELECT * FROM gold.tco_summary_month ORDER BY charge_month")
    if summary.empty:
        st.info("No TCO rows — needs Databricks + AWS data to attribute.")
        return

    latest = summary.iloc[-1]
    c1, c2, c3 = st.columns(3)
    c1.metric("Total TCO (latest)", f"${latest['total_cost']:,.0f}")
    c2.metric("DBU cost", f"${latest['dbu_cost']:,.0f}")
    c3.metric("Unattributed AWS", f"${latest['unattributed_infra_cost']:,.0f}")

    melted = summary.melt(
        id_vars="charge_month",
        value_vars=["dbu_cost", "attributed_infra_cost", "unattributed_infra_cost"],
        var_name="component",
        value_name="cost",
    )
    st.plotly_chart(
        px.bar(
            melted,
            x="charge_month",
            y="cost",
            color="component",
            title="Monthly TCO: DBU vs attributed infra vs unattributed AWS",
        ),
        use_container_width=True,
    )

    st.subheader("TCO by Databricks cluster (latest month)")
    clusters = gold_df(
        "SELECT cluster_id, compute_class, tco_basis, dbu_cost, infra_cost, tco_cost, "
        "infra_pct_of_tco FROM gold.tco_by_cluster_month "
        "WHERE charge_month = (SELECT max(charge_month) FROM gold.tco_by_cluster_month) "
        "ORDER BY tco_cost DESC"
    )
    st.dataframe(clusters, use_container_width=True, hide_index=True)

    st.subheader("TCO by EKS cluster (latest month)")
    eks = gold_df(
        "SELECT cluster_name, control_plane_cost, node_ec2_cost, node_ebs_cost, "
        "eks_tco, nodes_attributed FROM gold.tco_eks_by_cluster_month "
        "WHERE charge_month = (SELECT max(charge_month) FROM gold.tco_eks_by_cluster_month) "
        "ORDER BY eks_tco DESC"
    )
    st.dataframe(eks, use_container_width=True, hide_index=True)
