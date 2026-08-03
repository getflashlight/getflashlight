"""NiceGUI routing — one real ``@ui.page()`` route per page, replacing Streamlit's
``st.navigation()``. The provider-page set is data-driven from
:func:`flashlight.transform.catalog.discover_provider_groups`, discovered once when
:func:`build_pages` runs (at dashboard boot) — matching what ``app.py`` did on every
Streamlit rerun, just computed once instead of per-interaction.

Global date-range state does NOT persist across page navigations in this version
(NiceGUI has no built-in cross-page session dict the way Streamlit's
``st.session_state`` did) — each page that needs a range builds its own bounds and
defaults to the trailing 6 months on every load (see e.g.
``views/provider_focus.py::render``). Revisit only if that's felt as a real gap
once the app is in use.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import HTTPException
from nicegui import ui

from flashlight.core.settings import get_settings
from flashlight.dashboard import chrome
from flashlight.dashboard.data import (
    NO_DATA_MSG,
    gold_df,
    gold_last_updated,
    has_data,
    provider_label,
)
from flashlight.dashboard.data import to_date as _d
from flashlight.transform.catalog import discover_provider_groups


def range_has_partial_month(end: date) -> bool:
    """True when the selected end date falls in the still-accruing current month."""
    current = _d(gold_df("SELECT date_trunc('month', CURRENT_DATE) AS m").iloc[0]["m"])
    return end.replace(day=1) >= current


# ── Nav ──────────────────────────────────────────────────────────────────────
def _fixed_nav() -> tuple[tuple[str, str, str], ...]:
    """The always-present nav rows, minus the write-surface pages (Connections:
    edits connections.yml + spawns an ingest subprocess; Chat: BYOK key storage +
    outbound LLM calls) when running as a public demo (see ``build_pages``'s
    matching route gate), plus a Docs entry when a static docs site is mounted.
    """
    nav = [
        ("/", "Home", "home"),
        ("/connections", "Connections", "cable"),
        ("/chat", "Chat", "chat"),
    ]
    if get_settings().demo:
        nav = [item for item in nav if item[0] not in ("/connections", "/chat")]
    if get_settings().docs_dir:
        nav.append(("/docs", "Docs", "menu_book"))
    return tuple(nav)


def _nav_label(group: str) -> str:
    """AWS's own nav row reads "Redshift" — its dashboard page is Redshift-scoped
    only (see redshift_focus.py) even though the underlying gold group is still
    "aws" (Redshift's FOCUS rows live in gold/aws/, provider_name='AWS').
    """
    return "Redshift" if group == "aws" else f"{provider_label(group)} spend"


def _logo() -> None:
    with ui.row().classes("items-center gap-2"):
        ui.icon("flashlight_on", size="1.3rem").style(f"color:{chrome.ACCENT}")
        ui.label("Flashlight").classes("text-sm font-semibold").style(f"color:{chrome.INK_PRIMARY}")


def _nav_row(*, label: str, icon: str, href: str, active: bool) -> None:
    row = ui.row().classes("items-center gap-2 w-full px-2 py-2 cursor-pointer").style(
        f"border-radius:8px;background:{chrome.SURFACE if active else 'transparent'};"
    )
    with row:
        ui.icon(icon, size="1.1rem").style(f"color:{chrome.INK_SECONDARY}")
        ui.link(label, href).classes("text-sm no-underline").style(f"color:{chrome.INK_PRIMARY}")
    row.on("click", lambda: ui.navigate.to(href))


def shell(active_path: str) -> ui.column:
    """Page chrome (left drawer nav + header) shared by every page. Returns the
    content container to build the page body inside — call as ``with shell(path):``.
    """
    ui.dark_mode().enable()
    ui.add_head_html(chrome.HEAD_CSS)

    groups = discover_provider_groups()
    with ui.left_drawer(fixed=True).style(
        f"background:{chrome.PAGE};border-right:1px solid {chrome.BORDER};padding:16px 8px;"
    ):
        ui.label("OVERVIEW").classes("text-xs font-semibold px-2 mb-1").style(
            f"color:{chrome.INK_MUTED}"
        )
        for href, label, icon in _fixed_nav():
            _nav_row(label=label, icon=icon, href=href, active=active_path == href)
        if groups:
            ui.label("BY PROVIDER").classes("text-xs font-semibold px-2 mb-1 mt-3").style(
                f"color:{chrome.INK_MUTED}"
            )
            for group in groups:
                href = f"/{group}"
                _nav_row(
                    label=_nav_label(group),
                    icon="cloud",
                    href=href,
                    active=active_path == href,
                )

    with ui.header().classes("items-center justify-between px-6 py-3").style(
        f"background:{chrome.PAGE};border-bottom:1px solid {chrome.BORDER};"
    ):
        with ui.row().classes("items-center gap-4"):
            _logo()
            updated = gold_last_updated()
            if updated:
                ui.label(f"Data updated · {updated:%Y-%m-%d %H:%M} UTC").classes("text-xs").style(
                    f"color:{chrome.INK_MUTED}"
                )

    return ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-8")


def no_data_page(title: str) -> None:
    ui.label(title).classes("text-lg font-semibold").style(f"color:{chrome.INK_PRIMARY}")
    ui.label(NO_DATA_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


def build_pages() -> None:
    """Register every ``@ui.page()`` route. Call once, before ``ui.run()``."""
    from nicegui import app

    from flashlight.dashboard.views import (
        chat,
        connections,
        driver_health,
        efficiency_waste,
        home_overview,
        policy,
        provider_focus,
        redshift_focus,
        usage,
    )

    settings = get_settings()

    @ui.page("/")
    def _home() -> None:
        with shell("/"):
            if not has_data():
                no_data_page("Flashlight")
            else:
                home_overview.render()

    # Connections (edits connections.yml, spawns an ingest subprocess) and Chat
    # (BYOK key storage, outbound LLM calls) are the dashboard's only write/mutation
    # surfaces — routes aren't registered at all in demo mode, so they 404 instead
    # of just being hidden from nav (see _fixed_nav above).
    if not settings.demo:

        @ui.page("/connections")
        def _connections() -> None:
            with shell("/connections"):
                connections.render()

        @ui.page("/chat")
        async def _chat() -> None:
            with shell("/chat"):
                await chat.render()

    if settings.docs_dir and Path(settings.docs_dir).is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/docs", StaticFiles(directory=settings.docs_dir, html=True), name="docs")

    @ui.page("/usage")
    def _usage() -> None:
        with shell("/usage"):
            usage.render()

    def _render_databricks_page(label: str) -> None:
        """Efficiency & waste, client driver health, and policy compliance are, in
        practice, Databricks signals (the only two waste_record producers are
        Databricks + Redshift, and Redshift's waste already lives on its own
        Redshift-scoped page — see redshift_focus.py; driver_health and policy
        compliance each have exactly one producer: Databricks). So they're nested
        here as extra tabs rather than separate top-level nav entries.
        """
        provider_focus.render(
            "databricks",
            label,
            extra_tabs=[
                ("Efficiency & Waste", lambda: efficiency_waste.render("Databricks")),
                ("Client Driver Health", driver_health.render),
                ("Policy Compliance", policy.render),
            ],
        )

    # One parameterized route, not one @ui.page per group discovered right now —
    # discover_provider_groups() reads gold/ live, so it can (and does) return a
    # different answer *after* the dashboard has booted: a provider's first sync
    # publishes GOLD for it, and the nav (built fresh on every render — see
    # shell() above) immediately shows a link for it. A group-at-a-time loop here
    # only registers routes for what existed at boot, so that new nav link 404s
    # until the dashboard process is restarted. Checking membership per request
    # instead means a group's page and its nav link agree at all times.
    @ui.page("/{group}")
    def _provider_page(group: str) -> None:
        if group not in discover_provider_groups():
            raise HTTPException(status_code=404)
        label = provider_label(group)
        with shell(f"/{group}"):
            if not has_data():
                no_data_page(f"{label} spend")
            elif group == "aws":
                redshift_focus.render()
            elif group == "databricks":
                _render_databricks_page(label)
            else:
                provider_focus.render(group, label)
