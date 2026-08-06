"""Launch the NiceGUI dashboard in-process.

``flashlight dashboard serve`` registers every ``@ui.page()`` route
(:func:`flashlight.dashboard.router.build_pages`) then calls ``ui.run()`` —
NiceGUI serves its own FastAPI/Uvicorn app directly, no subprocess needed (unlike
the old Streamlit launcher, which shelled out to drive Streamlit's own CLI runtime).
Host/port come from settings.
"""

from __future__ import annotations

import os
import socket

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.lake import paths

logger = get_logger(__name__)


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on *host:port*.

    Public because ``dashboard/mcp_runner.py`` asks the same question of the MCP port —
    a server started in a terminal has to read as up on the dashboard's MCP page, and
    the port is the only evidence that crosses process boundaries.
    """
    connect_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    try:
        with socket.create_connection((connect_host, port), timeout=0.5):
            return True
    except OSError:
        return False


def prepare_storage_path() -> None:
    """Point ``NICEGUI_STORAGE_PATH`` at the lake, best-effort, before NiceGUI imports.

    NiceGUI reads that env var once at import time and only mkdir's one level itself — so
    the directory has to exist up front. It lives under ``FLASHLIGHT_HOME`` rather than
    NiceGUI's CWD-relative ``.nicegui/`` default so it travels with the lake instead of
    with whatever directory the CLI happened to run from.

    Nothing durable depends on this any more: the assistant's provider/model/base URL moved
    to ``<home>/config/assistant.yml``
    (:mod:`flashlight.dashboard.assistant_config`) precisely because a container points
    ``NICEGUI_STORAGE_PATH`` at /tmp, which forgot them on every restart. What's left here is
    per-tab scratch (``app.storage.tab``, e.g. a key whose keychain write failed), so a
    degraded path costs a tab's worth of state, not a setting.

    Two deliberate escape hatches for a read-only lake home:

    * a caller-set ``NICEGUI_STORAGE_PATH`` wins outright (a container points it at /tmp);
    * the mkdir is best-effort. It used to run unconditionally, so an unwritable lake home
      failed the dashboard at boot, before it served anything.
    """
    if "NICEGUI_STORAGE_PATH" in os.environ:
        return
    storage_dir = paths.meta_dir() / "dashboard_storage"
    try:
        storage_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "dashboard_storage_unwritable",
            path=str(storage_dir),
            error=str(exc),
            hint="set NICEGUI_STORAGE_PATH to a writable path; per-tab dashboard state "
            "won't be kept until then (assistant settings live in config/assistant.yml "
            "and are unaffected)",
        )
    else:
        os.environ["NICEGUI_STORAGE_PATH"] = str(storage_dir)


def serve_dashboard() -> None:
    """Run the dashboard on the configured host/port (blocks until stopped)."""
    settings = get_settings()

    # Preflight: a busy port otherwise surfaces as an opaque "address already in
    # use" traceback from uvicorn. Fail fast with an actionable message instead.
    if port_in_use(settings.dashboard_host, settings.dashboard_port):
        port = settings.dashboard_port
        logger.error("dashboard_port_in_use", host=settings.dashboard_host, port=port)
        raise SystemExit(
            f"Port {port} is already in use — the dashboard may already be running.\n"
            f"  • Open the running one:  http://127.0.0.1:{port}\n"
            f"  • Or free the port:      lsof -nP -iTCP:{port} -sTCP:LISTEN   then kill <PID>\n"
            f"  • Or pick another port:  FLASHLIGHT_DASHBOARD_PORT=8502 flashlight dashboard serve"
        )

    prepare_storage_path()

    from nicegui import ui

    from flashlight.dashboard.chrome import FAVICON_SVG
    from flashlight.dashboard.router import build_pages

    build_pages()

    logger.info(
        "dashboard_starting",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
    )
    ui.run(
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        title="Flashlight",
        favicon=FAVICON_SVG,
        dark=True,
        reload=False,
        # Opening a browser only makes sense when we're on the same machine as the
        # user's browser. Binding a non-loopback host means we aren't (a container, a
        # remote box), where `show=True` just tries and fails to launch a browser.
        show=settings.dashboard_host in ("127.0.0.1", "localhost", "::1"),
    )
