"""Trigger ``flashlight ingest`` from the dashboard, as a subprocess.

The dashboard process itself stays read-only (see ``dashboard/launch.py``'s
docstring) — this shells out to the same CLI entrypoint a terminal user would
run, rather than calling ``ingest/runner.py::run_ingest`` in-process, so
``ingest`` stays the sole writer regardless of who launched it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from flashlight.dashboard.connection_credentials import load_secret
from flashlight.ingest.config import load_all_connections


def _secrets_env(connections_path: Path) -> dict[str, str]:
    """Env vars for every configured ``*_env`` field with a keychain-stored secret."""
    env: dict[str, str] = {}
    for cfg in load_all_connections(str(connections_path)):
        for field in cfg.model_fields:
            if not field.endswith("_env"):
                continue
            env_name = getattr(cfg, field, None)
            if not env_name:
                continue
            value = load_secret(env_name)
            if value:
                env[env_name] = value
    return env


def sync_now(
    connections_path: Path, *, full_refresh: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run ``flashlight ingest`` against *connections_path*, blocking.

    Call via ``nicegui.run.io_bound`` from an async click handler — this is a
    long-running, blocking call and must not run directly in the event loop.
    """
    env = {**os.environ, **_secrets_env(connections_path)}
    cmd = [sys.executable, "-m", "flashlight.cli", "ingest", "--connections", str(connections_path)]
    if full_refresh:
        cmd.append("--full-refresh")
    return subprocess.run(cmd, env=env, capture_output=True, text=True)
