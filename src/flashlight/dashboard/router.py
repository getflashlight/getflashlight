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
from fastapi.responses import RedirectResponse, Response
from nicegui import ui

from flashlight.core.settings import get_settings
from flashlight.dashboard import chrome
from flashlight.dashboard.data import (
    NO_DATA_MSG,
    gold_df,
    gold_last_updated,
    gold_session,
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
    """The always-present nav rows, minus the pages a public demo must not offer
    (Connections: edits connections.yml + spawns an ingest subprocess; Assistant: BYOK key
    storage + outbound LLM calls; MCP server: spawns a process that opens an
    unauthenticated port — which ``mcp/server.py`` refuses to do in demo mode anyway) —
    see ``build_pages``'s matching route gate. Plus a Docs entry when a static docs site
    is mounted.
    """
    # Home (read-only) first, write surfaces after it — the demo filter below strips the
    # tail. Home is the only cross-provider page there is: utilization and the owner/tag
    # leaderboards used to sit here too, and are now tabs on each provider page (see
    # _RETIRED_ROUTES).
    nav = [
        ("/", "Home", "home"),
        ("/connections", "Connections", "cable"),
        ("/assistant", "Assistant", "assistant"),
        ("/mcp-server", "MCP server", "hub"),
    ]
    if get_settings().demo:
        nav = [
            item
            for item in nav
            if item[0] not in ("/connections", "/assistant", "/mcp-server")
        ]
    if get_settings().docs_dir:
        nav.append(("/docs", "Docs", "menu_book"))
    return tuple(nav)


# Provider groups that lead the "BY PROVIDER" nav, in this order, ahead of every
# remaining group in alphabetical order. Databricks is the platform users come here
# for; the cloud underneath it reads as supporting detail, so it sorts first even
# though "aws" would win alphabetically. Snowflake is always listed (even before its
# first GOLD publish) so the visibility UX is reachable with synthetic fallback.
_NAV_GROUP_ORDER: tuple[str, ...] = ("databricks", "snowflake")

_SIDEBAR_PROVIDER_LOGOS: dict[str, str] = {
    "databricks": chrome.CONNECTOR_LOGOS["databricks"],
    "aws": chrome.CONNECTOR_LOGOS["redshift"],
    "snowflake": chrome.CONNECTOR_LOGOS["snowflake"],
}


def _nav_groups() -> list[str]:
    """Provider groups in nav order: :data:`_NAV_GROUP_ORDER` first, then the rest
    in the alphabetical order ``discover_provider_groups`` already returns.

    Snowflake is always included so ``/snowflake`` stays reachable before the first
    ingest publishes ``gold/snowflake/`` (visibility falls back to synthetic Parquet).
    """
    groups = discover_provider_groups()
    if "snowflake" not in groups:
        groups = [*groups, "snowflake"]
    lead = [g for g in _NAV_GROUP_ORDER if g in groups]
    return lead + [g for g in groups if g not in lead]


def _nav_label(group: str) -> str:
    """A provider's display name (data.provider_label), bare — the "BY PROVIDER"
    heading above these rows already says they're spend pages, so "Databricks" beats
    "Databricks spend". AWS reads "AWS Redshift", matching its Redshift-scoped page
    (see redshift_focus.py) even though the gold group is still "aws" and its rows
    still say provider_name='AWS'.
    """
    return provider_label(group)


def _logo() -> None:
    with ui.row().classes("items-center gap-2"):
        ui.image(chrome.LOGO_DATA_URL).classes("w-5 h-5 flex-none").props("fit=contain no-spinner")
        ui.label("Flashlight").classes("text-sm font-semibold").style(f"color:{chrome.INK_PRIMARY}")


def _nav_row(
    *, label: str, icon: str, href: str, active: bool, logo_source: str | None = None
) -> None:
    row = ui.row().classes(
        "fl-sidebar-row items-center gap-2 w-full px-2 py-2 cursor-pointer"
        + (" fl-sidebar-active" if active else "")
    ).style(
        f"border-radius:8px;background:{chrome.SURFACE if active else 'transparent'};"
    )
    with row:
        if logo_source:
            ui.image(logo_source).classes("flex-none").style(
                "width:1.1rem;height:1.1rem;"
            ).props("fit=contain no-spinner")
        else:
            ui.icon(icon, size="1.1rem").style(f"color:{chrome.INK_SECONDARY}")
        ui.link(label, href).classes("fl-sidebar-label text-sm no-underline").style(
            f"color:{chrome.INK_PRIMARY}"
        )
    row.on("click", lambda: ui.navigate.to(href))


def shell(active_path: str, *, full_height: bool = False) -> ui.column:
    """Page chrome (left drawer nav + header) shared by every page. Returns the
    content container to build the page body inside — call as ``with shell(path):``.

    *full_height* swaps the usual scroll-down-a-report container (centered,
    max-w-6xl, padded, gap-8) for one that fills the viewport below the header
    with no padding of its own — for the assistant page, which owns its whole
    vertical space (a scrolling transcript with a composer pinned under it,
    chat-app style) rather than being one more stacked report section.
    """
    ui.dark_mode().enable()
    ui.add_head_html(chrome.HEAD_ICONS)
    ui.add_head_html(chrome.HEAD_CSS)
    if full_height:
        ui.add_body_html('<script>document.body.classList.add("fl-full-height")</script>')

    groups = _nav_groups()
    # value=True (rather than the default None) skips NiceGUI's post-connect
    # run_javascript round trip that asks the browser whether the drawer
    # should start open based on viewport width. The compact-rail control below
    # owns the sidebar state, avoiding that fragile first-paint trip.
    drawer = ui.left_drawer(value=True, fixed=True).props("mini-width=64")
    with drawer.style(
        f"background:{chrome.PAGE};border-right:1px solid {chrome.BORDER};padding:16px 8px;"
    ):
        # A small edge control reads as a sidebar-width affordance, rather than as
        # another destination in the navigation list.
        sidebar_is_compact = False

        def _toggle_sidebar() -> None:
            nonlocal sidebar_is_compact
            sidebar_is_compact = not sidebar_is_compact
            if sidebar_is_compact:
                drawer.props("mini")
                sidebar_toggle.props("icon=chevron_right")
            else:
                drawer.props(remove="mini")
                sidebar_toggle.props("icon=chevron_left")

        sidebar_toggle = ui.button(icon="chevron_left", on_click=_toggle_sidebar).props(
            "flat dense round"
        ).classes("fl-sidebar-toggle")

        with ui.column().classes("w-full h-full no-wrap"):
            with ui.column().classes("w-full gap-0"):
                ui.label("OVERVIEW").classes(
                    "fl-sidebar-heading text-xs font-semibold px-2 mb-1"
                ).style(
                    f"color:{chrome.INK_MUTED}"
                )
                for href, label, icon in _fixed_nav():
                    _nav_row(label=label, icon=icon, href=href, active=active_path == href)
                if groups:
                    ui.separator().classes("fl-sidebar-group-divider").style(
                        f"background:{chrome.BORDER}"
                    )
                    ui.label("BY PROVIDER").classes(
                        "fl-sidebar-heading text-xs font-semibold px-2 mb-1 mt-3"
                    ).style(
                        f"color:{chrome.INK_MUTED}"
                    )
                    for group in groups:
                        href = f"/{group}"
                        _nav_row(
                            label=_nav_label(group),
                            icon="cloud",
                            href=href,
                            active=active_path == href,
                            logo_source=_SIDEBAR_PROVIDER_LOGOS.get(group),
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

    if full_height:
        # flex:1 against the flex-column .q-page set up in HEAD_CSS — takes
        # whatever height is left under the real header without naming it here.
        return ui.column().classes("w-full p-0 gap-0").style("flex:1;min-height:0;")
    return ui.column().classes("w-full max-w-6xl mx-auto p-6 gap-8")


# Icon paths browsers ask for on their own, with no page linking them: Safari probes
# /apple-touch-icon.png and /apple-touch-icon-precomposed.png at the site root, and
# browsers still request /favicon.ico even when the page declares a favicon <link>.
# NiceGUI only auto-creates a /favicon.ico route when the favicon is a *file* — ours is
# an inline SVG (chrome.FAVICON_SVG) inlined as a data: URL in the page head — so all
# three fell through to the "/{group}" catch-all page below, each logging
# "http://…/apple-touch-icon.png not found" (nicegui.py's 404 handler) and rendering an
# HTML error page to something expecting an image. Serve the same monogram at each.
#
# /favicon.ico needs this too even though NiceGUI does register it: it adds that route on
# *startup* (nicegui.py's _startup), i.e. appended after the page routes, and Starlette
# matches in registration order — so the catch-all got there first.
_ICON_ROUTES = ("/favicon.ico", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png")
_icon_routes_registered = False
_dashboard_assets_registered = False


def register_dashboard_assets() -> None:
    """Expose small, bundled dashboard images at a stable application URL."""
    global _dashboard_assets_registered
    if _dashboard_assets_registered:
        return

    from nicegui import app

    app.add_static_files("/dashboard-assets", Path(__file__).parent / "assets")
    _dashboard_assets_registered = True


def icon_response() -> Response:
    """The favicon monogram as an image response — served at every path in
    :data:`_ICON_ROUTES`."""
    return Response(chrome.FAVICON_SVG, media_type="image/svg+xml")


def register_icon_routes() -> None:
    """Serve the favicon monogram at the icon paths browsers probe. Idempotent, and must
    run before the page routes so it wins over the ``/{group}`` catch-all."""
    global _icon_routes_registered
    if _icon_routes_registered:
        return

    from nicegui import app

    for path in _ICON_ROUTES:
        app.get(path, include_in_schema=False)(icon_response)
    _icon_routes_registered = True


# Page URLs that used to exist and don't any more. An already-open tab, a bookmark, or
# a browser history/autocomplete entry keeps requesting them long after the page is
# gone, and the "/{group}" catch-all below answers those with a 404 error page plus a
# "http://…/tco not found" warning per request. Redirect to the nearest surviving page
# instead. Same register-before-the-page-routes requirement as _ICON_ROUTES above, and
# the same reason: Starlette matches in registration order, so the catch-all wins
# otherwise.
_RETIRED_ROUTES: dict[str, str] = {
    # TCO (Databricks DBU + attributed AWS infra) was removed along with the
    # silver.tco_* views and the gold/shared group. Home is the closest thing left: the
    # cross-provider spend overview.
    "/tco": "/",
    # Utilization and the owner/tag leaderboards were three views of one telemetry plane
    # (metrics.efficiency_record at its measurement, verdict and attribution stages) shown
    # as cross-provider pages. They are now the "Efficiency & Waste" and "Attribution"
    # tabs on every provider page. Home rather than a provider page because there is no one
    # provider to pick, and Home links to all of them.
    "/utilization": "/",
    "/leaderboard": "/",
}


def register_retired_route_redirects() -> None:
    """Redirect retired page URLs (:data:`_RETIRED_ROUTES`) to their nearest surviving
    page. Idempotent, and must run before the page routes so it wins over the
    ``/{group}`` catch-all.

    Idempotency is checked against the live route table rather than a module-level
    "already did it" flag, because :func:`build_pages` can legitimately run more than
    once against a *fresh* registry (that's what the NiceGUI test simulation does) — a
    flag would skip the re-registration and quietly hand these paths back to the
    catch-all.

    A temporary redirect (307, RedirectResponse's default) rather than a permanent one:
    301s get cached hard by browsers, which would outlive the entry being deleted from
    this map and make the URL unusable if it's ever wanted again.
    """
    from nicegui import app

    existing = {getattr(route, "path", None) for route in app.routes}
    for path, target in _RETIRED_ROUTES.items():
        if path in existing:
            continue

        # Bind the target per-iteration: a closure over `target` would resolve to the
        # loop's last value for every route.
        def _redirect(target: str = target) -> RedirectResponse:
            return RedirectResponse(target)

        app.get(path, include_in_schema=False)(_redirect)


def no_data_page(title: str) -> None:
    ui.label(title).classes("text-lg font-semibold").style(f"color:{chrome.INK_PRIMARY}")
    ui.label(NO_DATA_MSG).classes("text-sm").style(f"color:{chrome.INK_MUTED}")


def build_pages() -> None:
    """Register every ``@ui.page()`` route. Call once, before ``ui.run()``."""
    from nicegui import app

    # The read-only pages. `assistant` and `connections` are imported lazily inside the
    # non-demo branch below instead: importing `assistant` pulls in assistant_engine → keyring +
    # pydantic-ai → flashlight.mcp.server, so an eager import would load the entire
    # outbound-LLM and MCP-tool stack into a demo process that must never reach either.
    from flashlight.dashboard.views import (
        ai_costs,
        backing_compute,
        backing_storage,
        databricks_footprint,
        driver_health,
        home_overview,
        provider_focus,
        redshift_focus,
    )

    settings = get_settings()

    # Before any page route: "/{group}" below is a catch-all that would otherwise
    # swallow /favicon.ico, the apple-touch-icon probes, and the retired page URLs
    # stale tabs/bookmarks still ask for.
    register_icon_routes()
    register_dashboard_assets()
    register_retired_route_redirects()

    @ui.page("/")
    def _home() -> None:
        # One registered connection for the whole render, reused by every gold_df()
        # call inside it — see data.gold_session's docstring.
        with gold_session(), shell("/"):
            if not has_data():
                no_data_page("Flashlight")
            else:
                home_overview.render()

    # Every literal top-level route must be registered explicitly: `/{group}` below is a
    # catch-all, so an unregistered `/foo` lands in _provider_page and 404s there instead.
    # Connections (edits connections.yml, spawns an ingest subprocess) and Assistant
    # (BYOK key storage, outbound LLM calls, in-process MCP tool calls including
    # run_sql) are the dashboard's only write/mutation surfaces — routes aren't
    # registered at all in demo mode, so they 404 instead of just being hidden from
    # nav (see _fixed_nav above). /usage belongs to this group too: it reads the assistant
    # turn log, its only inbound link is a button on the assistant page, and its Parquet
    # root doesn't exist in the demo image — so leaving it registered meant the demo
    # advertised a page that 404s. /mcp-server joins them: it launches a subprocess that
    # opens an unauthenticated port serving ad-hoc SQL, which `mcp serve` itself refuses
    # to do under FLASHLIGHT_DEMO — so the page would only ever show that refusal.
    if not settings.demo:
        from flashlight.dashboard.views import assistant, connections, mcp_server, usage

        @ui.page("/connections")
        def _connections() -> None:
            with shell("/connections"):
                connections.render()

        @ui.page("/assistant")
        async def _assistant() -> None:
            with shell("/assistant", full_height=True):
                await assistant.render()

        @ui.page("/usage")
        def _usage() -> None:
            with shell("/usage"):
                usage.render()

        @ui.page("/mcp-server")
        async def _mcp_server() -> None:
            with shell("/mcp-server"):
                await mcp_server.render()

    if settings.docs_dir and Path(settings.docs_dir).is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/docs", StaticFiles(directory=settings.docs_dir, html=True), name="docs")

    def _render_snowflake_visibility_only() -> None:
        """Snowflake visibility UX without GOLD spend yet — synthetic Parquet fallback.

        No live Snowflake connection is made from the dashboard. Leaderboard + Visibility
        read ``snowflake/synthetic_data/*.parquet`` until ``flashlight ingest`` publishes
        ``gold/snowflake/``.
        """
        from flashlight.dashboard.snowflake.views import visibility as snowflake_visibility

        with ui.tabs().classes("w-full").props("dense") as tabs:
            tab_exec = ui.tab("LeaderBoard")
            tab_vis = ui.tab("Visibility")
        loaded: dict[str, bool] = {"LeaderBoard": False}
        panels: dict[str, ui.tab_panel] = {}
        with ui.tab_panels(tabs, value=tab_exec).classes("w-full").style(
            "background:transparent;"
        ):
            panels["LeaderBoard"] = ui.tab_panel(tab_exec)
            panels["Visibility"] = ui.tab_panel(tab_vis)
        with panels["LeaderBoard"]:
            snowflake_visibility.render_leaderboard()
        loaded["LeaderBoard"] = True

        def _on_tab_change(e: object) -> None:
            name = getattr(e, "value", None)
            if name == "Visibility" and name not in loaded:
                loaded[name] = True
                with panels["Visibility"]:
                    snowflake_visibility.render()

        tabs.on_value_change(_on_tab_change)

    def _render_snowflake_page(label: str) -> None:
        """FOCUS spend via provider_focus, plus Snowflake visibility + driver health."""
        from flashlight.dashboard.snowflake.views import visibility as snowflake_visibility

        provider_focus.render(
            "snowflake",
            label,
            extra_tabs=[
                ("LeaderBoard", snowflake_visibility.render_leaderboard),
                ("Visibility", snowflake_visibility.render),
                ("Client Driver Health", driver_health.render),
            ],
            show_alerts=False,
        )

    def _render_databricks_page(label: str) -> None:
        """Client driver health is nested here for Databricks (and likewise on the
        Snowflake page) — connectors implementing ``Connector.fetch_driver_health`` —
        rather than as a top-level nav entry.

        AI Costs is nested for the same reason: Databricks is the only connector emitting
        AI-categorized FOCUS rows today, and ``gold.ai_product_family`` maps Databricks'
        ``billing_origin_product`` enum specifically. Its view is provider-scoped, though,
        so when a connector for another provider's AI products lands (AWS Bedrock stamps
        the same FOCUS ``service_category``) that group's ``ai_spend_month`` populates with
        no SQL change and this can graduate to a core tab. Its window total also gets a KPI
        card, and unlike storage's that one is a *slice* of the net figure beside it, not an
        addition to it — same bill, same dollars, so the card says "part of" and keeps the
        default hue rather than claiming a colour of its own.

        Backing storage and Backing compute are nested on a slightly different basis: each
        has **two** producers (``aws_focus`` for the AWS cost, ``databricks`` for the
        Databricks-side map — Unity Catalog's bucket map for storage,
        ``system.compute.node_timeline``'s instance/cluster map for compute) but exactly
        one *subject* — the storage or compute behind this platform. The rule's purpose is
        to keep a page-specific tab off every other provider page, and that still holds.
        Their dollars are AWS-billed and stay out of ``databricks.monthly_bill`` and out of
        every figure derived from it, the ``Databricks net`` KPI included (see
        views/backing_storage.py, views/backing_compute.py and CLAUDE.md's "No
        cross-provider cost join"). They do get their own card *beside* those KPIs
        (``extra_kpis``) rather than only living a tab away, because "what does Databricks
        cost me?" is asked at the top of this page: each card names its biller, says it is
        not in net, and shares a hue for the same reason (a satellite AWS bill, not a slice
        of net). Side by side, never summed.

        ``databricks_footprint.footprint_card`` is the one deliberate exception to "never
        summed" — and it says so on its own face. It adds Net Spend + Backing storage +
        Backing compute into one "Total Databricks footprint" number, explicitly labelled
        and explicitly not the same thing as Net Spend (its subtitle names both
        components), because "what does running Databricks actually cost me end to end" is
        a real question this page didn't have an answer to otherwise. It sits right after
        Net Spend, before the individual satellite cards, and is omitted (not shown as
        merely equal to Net Spend) when neither backing plane has any mapped spend for the
        window.

        Spend detail (AI Costs, Databricks Storage, Databricks Compute) sits after
        Breakdown via ``after_breakdown``; Client Driver Health is supplied through
        ``extra_tabs``. Alerts are intentionally not shown on the Databricks page.

        Neither Efficiency & Waste nor Policy Compliance is in this list: both are core
        tabs on every provider page now (provider_focus.render), including providers with
        no telemetry, which get a named empty state instead of a hidden tab. Policy in
        particular was hiding rows while it was Databricks-only — every Redshift
        cluster-month already lands in ``policy.policy_record``.
        """
        provider_focus.render(
            "databricks",
            label,
            # The combined footprint card sits right after Net Spend (the two numbers
            # that answer "what did Databricks bill me" and "what does running it
            # actually cost, including AWS infra" belong next to each other), then the
            # in-bill slice, then the other-bill breakdowns — the row reads outward from
            # what Databricks charged (see each module's KPI_SUB/subtitle).
            extra_kpis=[
                databricks_footprint.footprint_card,
                ai_costs.kpi_card,
                backing_storage.kpi_card,
                backing_compute.kpi_card,
            ],
            after_breakdown=[
                ("AI Costs", ai_costs.render),
                ("Databricks Storage", backing_storage.render),
                ("Databricks Compute", backing_compute.render),
            ],
            extra_tabs=[
                ("Client Driver Health", driver_health.render),
            ],
            show_alerts=False,
        )

    # Explicit /snowflake before the catch-all: always reachable for the visibility UX
    # even when gold/snowflake/ has not been published yet.
    @ui.page("/snowflake")
    def _snowflake_page() -> None:
        label = provider_label("snowflake")
        with gold_session(), shell("/snowflake"):
            if "snowflake" in discover_provider_groups() and has_data():
                _render_snowflake_page(label)
            else:
                _render_snowflake_visibility_only()

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
        # /snowflake is registered explicitly above (visibility fallback before GOLD).
        # Starlette matches in registration order, so this catch-all should not see it;
        # if it does, mirror the dedicated page rather than 404.
        if group == "snowflake":
            label = provider_label("snowflake")
            with gold_session(), shell("/snowflake"):
                if "snowflake" in discover_provider_groups() and has_data():
                    _render_snowflake_page(label)
                else:
                    _render_snowflake_visibility_only()
            return
        if group not in discover_provider_groups():
            raise HTTPException(status_code=404)
        label = provider_label(group)
        # One registered connection for the whole render, reused by every gold_df()
        # call inside it (the Databricks page alone issues ~50 today) — see
        # data.gold_session's docstring.
        with gold_session(), shell(f"/{group}"):
            if not has_data():
                no_data_page(f"{label} spend")
            elif group == "aws":
                redshift_focus.render()
            elif group == "databricks":
                _render_databricks_page(label)
            else:
                provider_focus.render(group, label)
