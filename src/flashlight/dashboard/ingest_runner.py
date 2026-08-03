"""Trigger ``flashlight ingest`` from the dashboard, as a subprocess.

The dashboard process itself stays read-only (see ``dashboard/launch.py``'s
docstring) — this shells out to the same CLI entrypoint a terminal user would
run, rather than calling ``ingest/runner.py::run_ingest`` in-process, so
``ingest`` stays the sole writer regardless of who launched it. One ingest
approach, not two: the subprocess resolves its own connector secrets (real env
first, OS keychain fallback — see ``ingest/config.py``'s ``env()``), so there's
no separate "pre-populate the subprocess env from the keychain" step here to
keep in sync with that — a bare ``flashlight ingest`` run in a terminal
resolves the exact same secrets the same way.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from collections.abc import Callable
from datetime import date
from pathlib import Path

from flashlight.lake import paths


async def stream_sync(
    connections_path: Path,
    on_line: Callable[[str], None],
    *,
    full_refresh: bool = False,
    connector: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> tuple[int, str]:
    """Run ``flashlight ingest`` against *connections_path*, calling ``on_line``
    with each stdout/stderr line as the subprocess produces it (a live tail —
    users otherwise stare at a bare spinner for the whole sync), and returning
    ``(exit_code, run_id)`` once it finishes.

    A real ``asyncio`` subprocess, not ``nicegui.run.io_bound`` wrapping a
    blocking ``subprocess.run`` — awaiting its stdout naturally yields back to
    the event loop between lines, so the caller's ``on_line`` (pushing into a
    ``ui.log``) renders as output arrives instead of all at once at the end.

    ``connector`` restricts the run to one connector (the per-connection Sync
    button); omitted, every enabled connector runs (the "Sync now" button).
    ``start``/``end`` restrict the pull window; omitted, the CLI's own default
    (a 35-day lookback) applies.

    ``run_id`` is generated here (not left to the subprocess's own default) and
    passed through ``--run-id`` so it's known before the subprocess even starts —
    needed to name the saved-transcript file this function writes line-by-line
    as it tails stdout (:func:`flashlight.lake.paths.sync_log_path`), so the full
    output survives closing the live dialog that started it (see
    ``dashboard/views/connections.py``'s history section) — unlike the dialog's
    own in-memory ``lines`` list, this is flushed to disk per line, so even a
    sync that's killed mid-run (not just one that finishes) leaves a partial
    transcript behind instead of nothing.
    """
    run_id = uuid.uuid4().hex
    # PYTHONUNBUFFERED=1 is load-bearing here, not cosmetic: Python fully
    # block-buffers stdout (~8KB) whenever it isn't a real terminal, which a
    # piped subprocess never is. Without it the child accumulates output
    # silently and this coroutine's `async for` just... waits — the tail
    # freezes after whatever fit in the last flush (often just the first line)
    # until the buffer fills or the process exits, at which point everything
    # arrives in one dump. Exactly the "stuck at 0/1 connectors done" bug this
    # fixes.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    cmd = [
        sys.executable,
        "-m",
        "flashlight.cli",
        "ingest",
        "--connections",
        str(connections_path),
        "--run-id",
        run_id,
    ]
    if start is not None:
        cmd.extend(["--start", start.isoformat()])
    if end is not None:
        cmd.extend(["--end", end.isoformat()])
    if full_refresh:
        cmd.append("--full-refresh")
    if connector is not None:
        cmd.extend(["--connector", connector])
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    assert proc.stdout is not None
    log_path = paths.sync_log_path(run_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log_file:
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip("\n")
            log_file.write(line + "\n")
            log_file.flush()
            on_line(line)
    return await proc.wait(), run_id
