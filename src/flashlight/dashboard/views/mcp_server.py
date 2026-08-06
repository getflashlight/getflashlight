"""MCP server control — status, start/stop, live output, and what to point at it.

The MCP server is the agent-facing half of Flashlight and was terminal-only: you had
to know ``flashlight mcp serve`` existed, and once running there was nothing to look at.
This page is a control surface over the same subprocess
(:mod:`flashlight.dashboard.mcp_runner`), plus the two things you actually need next —
the endpoint to paste into a client, and the tool inventory that endpoint exposes.

The tool list is read from ``mcp.list_tools()`` in-process, the same source
``assistant_engine`` plans against, so it can't drift from what the server really serves
— there's no hand-maintained copy here.

Like Connections, this is a control surface, not a writer: the subprocess it starts is
read-only over GOLD.
"""

from __future__ import annotations

import pandas as pd
from nicegui import ui

from flashlight.core.settings import get_settings
from flashlight.dashboard import chrome, mcp_runner

_CLIENT_CONFIG_TEMPLATE = """{{
  "mcpServers": {{
    "flashlight": {{
      "type": "http",
      "url": "{endpoint}"
    }}
  }}
}}"""


def _client_config(endpoint: str) -> str:
    return _CLIENT_CONFIG_TEMPLATE.format(endpoint=endpoint)


async def _tool_rows() -> list[dict[str, str]]:
    """Name + first line of each tool's docstring, straight from the live server.

    Imported here rather than at module scope: ``flashlight.mcp.server`` pulls in the
    whole read stack, and the demo process must never import it at all (see
    ``router.build_pages``' demo gate and ``tests/test_demo_gate.py``).
    """
    from flashlight.mcp.server import mcp

    tools = await mcp.list_tools()
    return [
        {"name": t.name, "description": (t.description or "").strip().split("\n")[0]}
        for t in tools
    ]


async def render() -> None:
    settings = get_settings()
    endpoint = mcp_runner.endpoint()

    chrome.section_title("MCP server")
    chrome.section_caption(
        "Serves the published GOLD views to agents over MCP (streamable HTTP). Read-only: "
        "it queries the same Parquet the dashboard does and never writes to the lake."
    )

    @ui.refreshable
    def status_body() -> None:
        ours, listening = mcp_runner.is_running(), mcp_runner.is_listening()
        with chrome.panel(), ui.column().classes("w-full gap-3"):
            with ui.row().classes("w-full items-center gap-4"):
                chrome.status_badge(listening, labels=("Running", "Stopped"))
                ui.label(endpoint).classes("text-sm font-mono").style(
                    f"color:{chrome.INK_PRIMARY}"
                )
                ui.space()
                if ours:
                    ui.button("Stop", icon="stop", on_click=_stop).props(
                        "no-caps color=negative"
                    ).mark("mcp-stop")
                else:
                    ui.button("Start", icon="play_arrow", on_click=_start).props(
                        "no-caps color=primary"
                    ).mark("mcp-start")
            if listening and not ours:
                # Started elsewhere: we can see the port but hold no handle on the
                # process, so offering a Stop button here would be a lie.
                chrome.section_caption(
                    f"Something is already serving {settings.mcp_host}:{settings.mcp_port} — "
                    "started outside this dashboard (a terminal, a service manager). Stop it "
                    "where you started it; this page can't."
                )
            elif ours:
                chrome.section_caption(
                    "Started by this dashboard, so it exits when the dashboard does. Run "
                    "`flashlight mcp serve` in a terminal (or under a service manager) for a "
                    "server that outlives it."
                )
            else:
                chrome.section_caption(
                    "Not running. Anything pointed at the endpoint above will fail to connect "
                    "until you start it."
                )
            # Stated on the page, not just in the docs: this is the one Flashlight
            # process that opens a port, and mcp_host defaults to 0.0.0.0.
            chrome.section_caption(
                "No authentication: the server exposes ad-hoc read-only SQL over your lake to "
                f"anything that can reach it. FLASHLIGHT_MCP_HOST is {settings.mcp_host!r} — "
                "bind it to 127.0.0.1 unless you mean to expose it."
            )

    # Both refresh `log_body`, which is defined further down: safe because a click can only
    # happen after this whole function has run, but it is a free variable — don't call
    # either of these during the render itself.
    async def _start(_: object = None) -> None:
        if not await mcp_runner.start():
            # Lost the race with another tab, or something else took the port between the
            # last render and this click.
            ui.notify("Already running", type="warning")
            status_body.refresh()
            return
        log_body.refresh()
        status_body.refresh()

    async def _stop(_: object = None) -> None:
        await mcp_runner.stop()
        log_body.refresh()
        status_body.refresh()

    status_body()

    with chrome.panel():
        chrome.panel_title("Connect a client")
        chrome.section_caption(
            "Add this to your MCP client's server config (Claude Code: `claude mcp add "
            f"--transport http flashlight {endpoint}`)."
        )
        ui.code(_client_config(endpoint), language="json").classes("w-full")

    @ui.refreshable
    def log_body() -> None:
        chrome.panel_title("Server output")
        lines = mcp_runner.recent_lines()
        if not lines and not mcp_runner.is_running():
            # No empty 30vh box before there's anything to put in it — the panel appears
            # when the server does (both handlers refresh this).
            chrome.section_caption("Nothing yet — start the server to see its output here.")
            return
        log_widget = ui.log(max_lines=2000).classes("w-full").style(
            "height:30vh; font-size:12px;"
        )
        for line in lines:
            log_widget.push(line)

        client_gone = False

        def _on_line(line: str) -> None:
            # A server streams for as long as it runs, so the browser tab going away
            # mid-stream is the normal case, not an edge one: every element under it is
            # already torn down, and further pushes would raise once per line.
            nonlocal client_gone
            if client_gone:
                return
            try:
                log_widget.push(line)
            except RuntimeError:
                client_gone = True
                unsubscribe()

        unsubscribe = mcp_runner.subscribe(_on_line)
        ui.context.client.on_disconnect(unsubscribe)

    with chrome.panel():
        log_body()

    with chrome.panel():
        chrome.panel_title("Tools exposed")
        chrome.section_caption(
            "Read live from the server's own registry, so this list is what an agent "
            "actually sees."
        )
        chrome.flat_table(
            pd.DataFrame(await _tool_rows()),
            key="mcp-tools",
            rename={"name": "Tool", "description": "What it does"},
        )
