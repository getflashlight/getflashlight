"""Launch the NiceGUI dashboard in-process.

``flashlight dashboard serve`` registers every ``@ui.page()`` route
(:func:`flashlight.dashboard.router.build_pages`) then calls ``ui.run()`` —
NiceGUI serves its own FastAPI/Uvicorn app directly, no subprocess needed (unlike
the old Streamlit launcher, which shelled out to drive Streamlit's own CLI runtime).
Host/port come from settings.
"""

from __future__ import annotations

import socket

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings

logger = get_logger(__name__)


def _port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on *host:port*."""
    connect_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    try:
        with socket.create_connection((connect_host, port), timeout=0.5):
            return True
    except OSError:
        return False


def serve_dashboard() -> None:
    """Run the dashboard on the configured host/port (blocks until stopped)."""
    settings = get_settings()

    # Preflight: a busy port otherwise surfaces as an opaque "address already in
    # use" traceback from uvicorn. Fail fast with an actionable message instead.
    if _port_in_use(settings.dashboard_host, settings.dashboard_port):
        port = settings.dashboard_port
        logger.error("dashboard_port_in_use", host=settings.dashboard_host, port=port)
        raise SystemExit(
            f"Port {port} is already in use — the dashboard may already be running.\n"
            f"  • Open the running one:  http://127.0.0.1:{port}\n"
            f"  • Or free the port:      lsof -nP -iTCP:{port} -sTCP:LISTEN   then kill <PID>\n"
            f"  • Or pick another port:  FLASHLIGHT_DASHBOARD_PORT=8502 flashlight dashboard serve"
        )

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
        show=True,
    )
