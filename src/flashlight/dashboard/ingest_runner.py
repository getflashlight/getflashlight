"""Trigger ``flashlight ingest`` from the dashboard, as a subprocess.

The dashboard process itself stays read-only (see ``dashboard/launch.py``'s
docstring) — this shells out to the same CLI entrypoint a terminal user would
run, rather than calling ``ingest/runner.py::run_ingest`` in-process, so
``ingest`` stays the sole writer regardless of who launched it.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
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


async def stream_sync(
    connections_path: Path,
    on_line: Callable[[str], None],
    *,
    full_refresh: bool = False,
    connector: str | None = None,
) -> int:
    """Run ``flashlight ingest`` against *connections_path*, calling ``on_line``
    with each stdout/stderr line as the subprocess produces it (a live tail —
    users otherwise stare at a bare spinner for the whole sync), and returning
    its exit code once it finishes.

    A real ``asyncio`` subprocess, not ``nicegui.run.io_bound`` wrapping a
    blocking ``subprocess.run`` — awaiting its stdout naturally yields back to
    the event loop between lines, so the caller's ``on_line`` (pushing into a
    ``ui.log``) renders as output arrives instead of all at once at the end.

    ``connector`` restricts the run to one connector (the per-connection Sync
    button); omitted, every enabled connector runs (the "Sync now" button).
    """
    env = {**os.environ, **_secrets_env(connections_path)}
    cmd = [sys.executable, "-m", "flashlight.cli", "ingest", "--connections", str(connections_path)]
    if full_refresh:
        cmd.append("--full-refresh")
    if connector is not None:
        cmd.extend(["--connector", connector])
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        on_line(raw_line.decode(errors="replace").rstrip("\n"))
    return await proc.wait()
