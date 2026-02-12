"""Auralake Dashboard — Streamlit app with chat UI and overview page."""

from __future__ import annotations

import os
from datetime import date

import plotly.express as px
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE = os.environ.get("AURALAKE_API_URL", "http://localhost:8000")
_DEFAULT_API_KEY = os.environ.get("AURALAKE_API_KEY", "")


def _get_headers() -> dict[str, str]:
    api_key = st.session_state.get("api_key", "")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _api_get(path: str) -> dict | list | None:
    try:
        resp = requests.get(f"{API_BASE}{path}", headers=_get_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        st.error(f"API error: {e}")
        return None


def _api_post(path: str, body: dict) -> dict | None:
    try:
        resp = requests.post(
            f"{API_BASE}{path}", headers=_get_headers(), json=body, timeout=120
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        st.error(f"API error: {e}")
        return None


# ---------------------------------------------------------------------------
# Chart rendering
# ---------------------------------------------------------------------------


def _render_chart(chart: dict) -> None:
    """Render a ChartData dict as a Plotly figure."""
    chart_type = chart.get("chart_type", "bar")
    title = chart.get("title", "")
    data = chart.get("data", {})
    x = chart.get("x", "")
    y = chart.get("y", "")

    if not data or not x:
        return

    import pandas as pd

    df = pd.DataFrame(data)

    if chart_type == "bar":
        fig = px.bar(df, x=x, y=y, title=title)
    elif chart_type == "line":
        fig = px.line(df, x=x, y=y, title=title)
    elif chart_type == "pie":
        values_col = y if isinstance(y, str) else y[0]
        fig = px.pie(df, names=x, values=values_col, title=title)
    elif chart_type == "area":
        fig = px.area(df, x=x, y=y, title=title)
    elif chart_type == "heatmap":
        fig = px.density_heatmap(df, x=x, y=y, title=title)
    elif chart_type == "scatter":
        fig = px.scatter(df, x=x, y=y, title=title)
    elif chart_type == "histogram":
        fig = px.histogram(df, x=x, y=y, title=title)
    else:
        fig = px.bar(df, x=x, y=y, title=title)

    st.plotly_chart(fig, use_container_width=True)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def chat_page() -> None:
    """Main chat interface."""
    st.header("Chat with Auralake")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display chat history
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            for chart in msg.get("charts", []):
                _render_chart(chart)

    # Chat input
    if prompt := st.chat_input("Ask about your lakehouse costs..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Build history for API
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state.messages[:-1]
        ]

        with st.chat_message("assistant"):
            with st.spinner("Thinking..."):
                result = _api_post(
                    "/api/v1/chat",
                    {"message": prompt, "history": history},
                )

            if result:
                answer = result.get("answer", "")
                charts = result.get("charts", [])

                st.markdown(answer)
                for chart in charts:
                    _render_chart(chart)

                st.session_state.messages.append(
                    {"role": "assistant", "content": answer, "charts": charts}
                )
            else:
                fallback = "Sorry, I couldn't get a response. Check the API connection."
                st.markdown(fallback)
                st.session_state.messages.append(
                    {"role": "assistant", "content": fallback}
                )


def overview_page() -> None:
    """Static overview dashboard with key metrics."""
    from datetime import timedelta

    st.header("Overview")

    # Date range pickers (default: last ~3 months)
    today = date.today()
    default_from = today - timedelta(days=90)
    col_from, col_to = st.columns(2)
    with col_from:
        from_date = st.date_input("From", value=default_from)
    with col_to:
        to_date = st.date_input("To", value=today)

    summary = _api_get("/api/v1/data/summary")
    billing = _api_get(f"/api/v1/data/billing?from_date={from_date}&to_date={to_date}")

    if not summary or not billing:
        st.warning("Could not load data. Is the backend running?")
        return

    # KPI row
    total_cost = billing.get("total_cost_usd", 0)
    total_dbu = billing.get("total_dbu_usage", 0)

    col1, col2, col3, col4 = st.columns(4)
    if total_cost > 0:
        col1.metric("Total Spend", f"${total_cost:,.2f}")
    elif total_dbu > 0:
        col1.metric("Total DBUs", f"{total_dbu:,.0f}")
    else:
        col1.metric("Total Spend", "$0.00")
    col2.metric("Recommendations", summary.get("recommendations", 0))
    col3.metric("Clusters", summary.get("clusters", 0))
    col4.metric("Jobs", summary.get("jobs", 0))

    st.divider()

    # --- Charts ---
    by_sku_dbu = billing.get("by_sku_dbu", {})
    monthly_by_sku = billing.get("monthly_by_sku", [])
    date_range = billing.get("date_range")
    insights = billing.get("insights", [])
    use_dbu = total_cost == 0 and total_dbu > 0

    if monthly_by_sku:
        import re
        from datetime import datetime as _dt

        import pandas as pd

        value_col = "dbu_usage" if use_dbu else "cost_usd"
        value_label = "DBU Usage" if use_dbu else "Cost (USD)"

        # Identify top 10 SKUs by total
        chart_totals = by_sku_dbu if use_dbu else billing.get("by_sku", {})
        top_skus_sorted = sorted(chart_totals.items(), key=lambda x: x[1], reverse=True)[:10]
        top_sku_names = {s for s, _ in top_skus_sorted}

        def _shorten_sku(name: str) -> str:
            s = name
            for prefix in ("ENTERPRISE_", "STANDARD_", "PREMIUM_"):
                if s.startswith(prefix):
                    s = s[len(prefix):]
                    break
            s = re.sub(r"_(US|EU|AP)_[A-Z_]+$", "", s)
            return s[:40] if len(s) > 40 else s

        # --- Chart 1: Total by SKU (horizontal bar) ---
        if date_range:
            min_label = _dt.strptime(date_range["min"], "%Y-%m").strftime("%b %Y")
            max_label = _dt.strptime(date_range["max"], "%Y-%m").strftime("%b %Y")
            range_suffix = f" ({min_label}\u2013{max_label})"
        else:
            range_suffix = ""
        bar_base = "Total DBU Usage by SKU" if use_dbu else "Total Cost by SKU"
        bar_title = f"{bar_base}{range_suffix}"

        labels = [_shorten_sku(sku) for sku, _ in top_skus_sorted]
        values = [v for _, v in top_skus_sorted]
        df_bar = pd.DataFrame({"SKU": labels, value_label: values})
        fig_bar = px.bar(
            df_bar, x=value_label, y="SKU", orientation="h", title=bar_title,
        )
        fig_bar.update_layout(yaxis={"categoryorder": "total ascending"})
        fig_bar.update_traces(texttemplate="%{x:,.0f}", textposition="outside")
        st.plotly_chart(fig_bar, use_container_width=True)

        # --- Chart 2: Monthly trend per SKU (line chart, clean) ---
        df = pd.DataFrame(monthly_by_sku)
        df = df[df["sku"].isin(top_sku_names)]
        df["SKU"] = df["sku"].map(_shorten_sku)

        trend_title = "Monthly DBU Trend by SKU" if use_dbu else "Monthly Cost Trend by SKU"
        df_trend = df.groupby(["month", "SKU"])[value_col].sum().reset_index()
        fig_line = px.line(
            df_trend,
            x="month", y=value_col, color="SKU",
            title=trend_title,
            labels={"month": "Month", value_col: value_label},
            markers=True,
        )
        fig_line.update_layout(
            xaxis_tickangle=0,
            legend={"orientation": "h", "y": -0.25},
        )
        st.plotly_chart(fig_line, use_container_width=True)

        # --- Heatmap table: SKU x Month with MoM % change ---
        import plotly.graph_objects as go

        pivot = df_trend.pivot(index="SKU", columns="month", values=value_col).fillna(0)
        # Sort SKUs by total descending
        pivot = pivot.loc[pivot.sum(axis=1).sort_values(ascending=False).index]
        months_sorted = sorted(pivot.columns)
        pivot = pivot[months_sorted]

        # Compute MoM % change for each cell
        pct_change = pivot.copy()
        for i in range(1, len(months_sorted)):
            prev_col = months_sorted[i - 1]
            curr_col = months_sorted[i]
            pct_change[curr_col] = pivot.apply(
                lambda row, p=prev_col, c=curr_col: (
                    ((row[c] - row[p]) / row[p] * 100) if row[p] > 0 else 0.0
                ),
                axis=1,
            )
        pct_change[months_sorted[0]] = 0.0  # no prior month for first column

        # Format display text: "12,345 (+7.2%)"
        display_text = []
        for sku in pivot.index:
            row_text = []
            for j, m in enumerate(months_sorted):
                val = pivot.loc[sku, m]
                pct = pct_change.loc[sku, m]
                if j == 0 or pct == 0:
                    row_text.append(f"{val:,.0f}")
                else:
                    sign = "+" if pct > 0 else ""
                    row_text.append(f"{val:,.0f}<br><sub>{sign}{pct:.1f}%</sub>")
            display_text.append(row_text)

        # Month labels like "Nov 2025"
        month_labels = [
            _dt.strptime(m, "%Y-%m").strftime("%b %Y") for m in months_sorted
        ]

        # Build heatmap on the % change values (skip first month)
        z_values = pct_change[months_sorted].values.tolist()

        fig_heat = go.Figure(
            data=go.Heatmap(
                z=z_values,
                x=month_labels,
                y=list(pivot.index),
                text=display_text,
                texttemplate="%{text}",
                colorscale=[
                    [0, "rgb(34,139,34)"],      # green = decrease (good)
                    [0.5, "rgb(255,255,255)"],   # white = no change
                    [1, "rgb(220,38,38)"],        # red = increase (bad)
                ],
                zmid=0,
                colorbar={"title": "MoM %", "ticksuffix": "%"},
                hovertemplate=(
                    "SKU: %{y}<br>Month: %{x}<br>"
                    "MoM Change: %{z:.1f}%<extra></extra>"
                ),
            )
        )
        fig_heat.update_layout(
            title="Monthly Usage Heatmap (MoM % Change)",
            yaxis={"autorange": "reversed", "dtick": 1},
            xaxis={"dtick": 1, "side": "top"},
            height=60 + len(pivot.index) * 50,
            margin={"l": 10, "r": 10, "t": 60, "b": 10},
        )
        st.plotly_chart(fig_heat, use_container_width=True)

        # --- Top contributors table for the latest MoM change ---
        if insights:
            latest_prev = insights[0].get("prev_month", "")
            latest_curr = insights[0].get("curr_month", "")
            if latest_prev and latest_curr:
                prev_label = _dt.strptime(latest_prev, "%Y-%m").strftime("%b %Y")
                curr_label = _dt.strptime(latest_curr, "%Y-%m").strftime("%b %Y")
            else:
                prev_label, curr_label = "Prev", "Curr"
            contrib_rows = []
            for ins in insights:
                for c in ins.get("top_contributors", [])[:2]:
                    c_sign = "+" if c["change_dbu"] > 0 else ""
                    pct = c.get("change_pct", 0)
                    pct_sign = "+" if pct > 0 else ""
                    contrib_rows.append(
                        {
                            "SKU": _shorten_sku(ins["sku"]),
                            "Type": c["type"].title(),
                            "Resource": c.get("name") or c["id"],
                            f"{prev_label} DBUs": f"{c.get('prev_dbu', 0):,.0f}",
                            f"{curr_label} DBUs": f"{c['dbu']:,.0f}",
                            "Change": f"{c_sign}{c['change_dbu']:,.0f} ({pct_sign}{pct:.0f}%)",
                            "Reason": c.get("reason") or "",
                            "_abs_change": abs(c["change_dbu"]),
                        }
                    )
            if contrib_rows:
                contrib_rows.sort(key=lambda r: r["_abs_change"], reverse=True)
                df_contrib = pd.DataFrame(contrib_rows).drop(columns=["_abs_change"])
                st.caption(
                    f"Top contributors to change"
                    f" ({prev_label} \u2192 {curr_label}) — >20% change only"
                )
                st.dataframe(
                    df_contrib,
                    use_container_width=True,
                    hide_index=True,
                )

    # Additional counts
    col1, col2, col3 = st.columns(3)
    col1.metric("Billing Records", summary.get("billing_records", 0))
    col2.metric("Query Plans", summary.get("query_plans", 0))
    col3.metric("S3 Objects", summary.get("s3_inventory_objects", 0))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Auralake",
        page_icon="🏔️",
        layout="wide",
    )

    st.sidebar.title("Auralake")

    if "api_key" not in st.session_state:
        st.session_state.api_key = _DEFAULT_API_KEY

    st.sidebar.text_input(
        "API Key",
        type="password",
        key="api_key",
        help="Enter your Auralake API key (starts with al_).",
    )

    if not st.session_state.get("api_key"):
        st.info("Enter your Auralake API key in the sidebar to get started.")
        return

    page = st.sidebar.radio("Navigate", ["Chat", "Overview"])

    if page == "Chat":
        chat_page()
    else:
        overview_page()


if __name__ == "__main__":
    main()
else:
    main()
