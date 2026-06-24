"""Launch the Streamlit dashboard as a subprocess.

``auralake dashboard serve`` shells out to ``streamlit run app.py`` (rather than
importing Streamlit's internals) so the app gets Streamlit's normal script
runtime. Host/port come from settings.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from auralake.core.logging import get_logger
from auralake.core.settings import get_settings

logger = get_logger(__name__)


def serve_dashboard() -> None:
    """Run the dashboard on the configured host/port (blocks until stopped)."""
    settings = get_settings()
    app_path = Path(__file__).parent / "app.py"
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
    ]
    logger.info(
        "dashboard_starting",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
    )
    subprocess.run(cmd, check=True)
