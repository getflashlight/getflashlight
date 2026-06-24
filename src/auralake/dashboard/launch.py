"""Launch the Streamlit dashboard as a subprocess.

``auralake dashboard serve`` shells out to ``streamlit run app.py`` (rather than
importing Streamlit's internals) so the app gets Streamlit's normal script
runtime. Host/port come from settings.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from auralake.core.logging import get_logger
from auralake.core.settings import get_settings

logger = get_logger(__name__)


def _open_browser_when_ready(host: str, port: int, timeout: float = 30.0) -> None:
    """Wait until the server accepts connections, then open the default browser.

    Runs in a daemon thread so the blocking ``streamlit run`` stays in the
    foreground. ``0.0.0.0``/empty bind addresses are rewritten to ``127.0.0.1``
    for the browseable URL.
    """
    connect_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((connect_host, port), timeout=1.0):
                break
        except OSError:
            time.sleep(0.25)
    else:
        logger.warning("dashboard_browser_open_timeout", host=connect_host, port=port)
        return
    url = f"http://{connect_host}:{port}"
    logger.info("dashboard_opening_browser", url=url)
    webbrowser.open(url)


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
    app_path = Path(__file__).parent / "app.py"

    # Preflight: a busy port otherwise surfaces as an opaque CalledProcessError
    # traceback from Streamlit. Fail fast with an actionable message instead.
    if _port_in_use(settings.dashboard_host, settings.dashboard_port):
        port = settings.dashboard_port
        logger.error("dashboard_port_in_use", host=settings.dashboard_host, port=port)
        raise SystemExit(
            f"Port {port} is already in use — the dashboard may already be running.\n"
            f"  • Open the running one:  http://127.0.0.1:{port}\n"
            f"  • Or free the port:      lsof -nP -iTCP:{port} -sTCP:LISTEN   then kill <PID>\n"
            f"  • Or pick another port:  AURALAKE_DASHBOARD_PORT=8502 auralake dashboard serve"
        )
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app_path),
        "--server.address",
        settings.dashboard_host,
        "--server.port",
        str(settings.dashboard_port),
        "--server.headless",
        "true",
        # Bundle the lake-blue theme as CLI flags so it applies regardless of the
        # caller's cwd (a bundled .streamlit/config.toml wouldn't be found reliably).
        "--theme.base",
        "light",
        "--theme.primaryColor",
        "#0E7C86",
        "--theme.backgroundColor",
        "#ffffff",
        "--theme.secondaryBackgroundColor",
        "#f3f7fa",
        "--theme.textColor",
        "#1B2A36",
        "--browser.gatherUsageStats",
        "false",
    ]
    logger.info(
        "dashboard_starting",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
    )
    threading.Thread(
        target=_open_browser_when_ready,
        args=(settings.dashboard_host, settings.dashboard_port),
        daemon=True,
    ).start()
    subprocess.run(cmd, check=True)
