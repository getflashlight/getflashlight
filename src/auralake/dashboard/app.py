"""Streamlit entry script — run via ``auralake dashboard serve`` (see launch.py).

Navigation is built at runtime: a Home overview, the cross-provider TCO page,
then one page per published provider group (AWS, Databricks, …). The provider set
is discovered from what's on disk, so a newly-ingested provider appears with no
code change.
"""

from __future__ import annotations

from functools import partial

import streamlit as st

from auralake.dashboard.context import init_global_range
from auralake.dashboard.data import NO_DATA_MSG, gold_last_updated, has_data, provider_label
from auralake.dashboard.theme import inject_css
from auralake.dashboard.views import home_overview, provider_focus, tco_overview
from auralake.transform.catalog import discover_provider_groups

st.set_page_config(page_title="Auralake", page_icon="💧", layout="wide")
inject_css()

if not has_data():
    st.title("Auralake")
    st.info(NO_DATA_MSG)
else:
    st.sidebar.markdown("### Time range")
    global_rng = init_global_range()
    updated = gold_last_updated()
    if updated:
        st.sidebar.markdown(
            f'<p class="aura-sidebar-meta">Data updated · {updated:%Y-%m-%d %H:%M} UTC</p>',
            unsafe_allow_html=True,
        )
    if global_rng:
        start, end = global_rng
        st.sidebar.markdown(
            f'<p class="aura-sidebar-meta">Viewing · {start:%b %d, %Y} → {end:%b %d, %Y}</p>',
            unsafe_allow_html=True,
        )

    groups = discover_provider_groups()
    provider_pages = [
        st.Page(
            partial(provider_focus.render, group, provider_label(group)),
            title=f"{provider_label(group)} spend",
            icon="☁️",
            url_path=group,
        )
        for group in groups
    ]
    provider_by_group = dict(zip(groups, provider_pages, strict=True))
    tco_page = st.Page(
        tco_overview.render,
        title="Total cost (Databricks + AWS)",
        icon="🧮",
        url_path="tco-overview",
    )
    pages = {
        "Overview": [
            st.Page(
                partial(
                    home_overview.render,
                    tco_page=tco_page,
                    provider_pages=provider_by_group,
                ),
                title="Home",
                icon="🏠",
                default=True,
            ),
            tco_page,
        ],
        "By provider": provider_pages,
    }
    st.navigation(pages).run()
