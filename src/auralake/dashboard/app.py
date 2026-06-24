"""Streamlit entry script — run via ``auralake dashboard serve`` (see launch.py).

Navigation is wired explicitly with ``st.navigation`` over the page render
functions in :mod:`auralake.dashboard.views`.
"""

from __future__ import annotations

import streamlit as st

from auralake.dashboard.views import aws_focus, billing_overview, tco_overview

st.set_page_config(page_title="Auralake", page_icon="💧", layout="wide")

navigation = st.navigation(
    [
        st.Page(billing_overview.render, title="Billing overview", icon="💵", default=True),
        st.Page(tco_overview.render, title="TCO overview", icon="🧮"),
        st.Page(aws_focus.render, title="AWS FOCUS", icon="☁️"),
    ]
)
navigation.run()
