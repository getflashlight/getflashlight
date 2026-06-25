"""Streamlit entry script — run via ``auralake dashboard serve`` (see launch.py).

Navigation is built at runtime: one page per published provider group (AWS,
Databricks, …) rendered by :mod:`auralake.dashboard.views.provider_focus`, plus a
TCO page for the cross-provider ``shared`` group. The provider set is discovered
from what's on disk, so a newly-ingested provider appears as its own page with no
code change.
"""

from __future__ import annotations

from functools import partial

import streamlit as st

from auralake.dashboard.data import gold_df, has_data
from auralake.dashboard.theme import inject_css
from auralake.dashboard.views import provider_focus, tco_overview
from auralake.transform.catalog import discover_provider_groups

st.set_page_config(page_title="Auralake", page_icon="💧", layout="wide")
inject_css()


def _provider_label(group: str) -> str:
    """Human label for a group — the provider_name in its data, else the titled slug."""
    try:
        df = gold_df(f'SELECT provider_name FROM "{group}".monthly_bill LIMIT 1')
        if not df.empty and df["provider_name"].iloc[0]:
            return str(df["provider_name"].iloc[0])
    except Exception:  # noqa: BLE001 - fall back to a readable slug on any query issue
        pass
    return group.replace("_", " ").title()


if not has_data():
    st.title("Auralake")
    st.info("No data yet — run `auralake sample` or `auralake ingest`, then refresh.")
else:
    # One page per published provider group, then the cross-provider TCO page.
    pages = [
        st.Page(
            partial(provider_focus.render, group, _provider_label(group)),
            title=_provider_label(group),
            icon="☁️",
            url_path=group,
            default=(i == 0),
        )
        for i, group in enumerate(discover_provider_groups())
    ]
    pages.append(
        st.Page(tco_overview.render, title="TCO overview", icon="🧮", url_path="tco-overview")
    )
    st.navigation(pages).run()
