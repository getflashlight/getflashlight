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

The subprocess's lifetime is tracked at **module level**, the same pattern as
``mcp_runner.py``, and for the same reason: a click handler's coroutine is the
current browser tab's own asyncio task, and that task is cancelled the moment
its client disconnects — closing the log dialog, reloading, or (since every
page here is a real ``@ui.page()`` route, not a client-side router) simply
navigating to another page. A sync that only ran *inside* that task used to
die with it. :func:`start_sync` spawns the subprocess and hands its tail to a
module-level ``asyncio.create_task`` instead, so the running sync has nothing
to do with whichever tab happened to start it — it keeps streaming to its log
file and its subscribers (and, on completion, to the run log) regardless of
what the browser does next. :func:`stream_sync` is kept as an await-to-completion
wrapper around that for callers (and tests) that want the old direct contract;
cancelling *that* await still only detaches the caller, never the sync itself.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from flashlight.ingest.config import load_connections
from flashlight.lake import paths

# Recent output for the current/last run, kept so a page opened (or reopened, after
# navigating away and back) mid-sync still shows context instead of an empty log
# until the next line happens to arrive.
_TAIL_LINES = 2000


@dataclass
class _Run:
    run_id: str
    connector: str | None
    total: int
    full_refresh: bool
    started_at: datetime
    # Its own Event/exit_code, not a shared module-level pair: a bare module-level
    # ``asyncio.Event()`` binds to whichever event loop first calls ``.wait()``/
    # ``.set()`` on it and raises on any other — fatal for tests, each of which runs
    # its own ``asyncio.run()`` (its own loop). One fresh Event per run, created
    # while *that* run's loop is already the running one (inside `start_sync`),
    # sidesteps it entirely.
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    exit_code: int | None = None


_proc: asyncio.subprocess.Process | None = None
_tail_task: asyncio.Task[None] | None = None
_current: _Run | None = None
_recent: deque[str] = deque(maxlen=_TAIL_LINES)
_subscribers: list[Callable[[str], None]] = []


def is_running() -> bool:
    """True if a sync started from this dashboard process is still going."""
    return _proc is not None and _proc.returncode is None


def current_run() -> _Run | None:
    """Metadata for the in-progress sync, or ``None`` if nothing is running.

    Lets a page reopened mid-sync (a fresh render — NiceGUI page functions rerun
    per client, there's no surviving local state to read) rebuild the same
    progress dialog instead of looking like nothing is happening.
    """
    return _current if is_running() else None


def recent_lines() -> list[str]:
    """The current (or, once it finishes, most recently finished) run's tail so far."""
    return list(_recent)


def subscribe(on_line: Callable[[str], None]) -> Callable[[], None]:
    """Register *on_line* for every subsequent output line; returns an unsubscribe.

    A list of callbacks rather than one, because the sync survives page
    navigation and dialog close/reopen — whoever's currently watching (zero, one,
    or several tabs) all get the same lines as they arrive.
    """
    _subscribers.append(on_line)

    def _unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            _subscribers.remove(on_line)

    return _unsubscribe


def _emit(line: str) -> None:
    _recent.append(line)
    for callback in list(_subscribers):
        try:
            callback(line)
        except Exception:  # noqa: BLE001 - one torn-down tab must not stop the others
            with contextlib.suppress(ValueError):
                _subscribers.remove(callback)


async def _tail(proc: asyncio.subprocess.Process, run: _Run, log_path: Path) -> None:
    """Pump the child's merged stdout/stderr into the log file and the subscribers,
    then record its exit code — detached from whichever caller started it (see the
    module docstring for why that detachment is the whole point).
    """
    global _proc
    assert proc.stdout is not None
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log_file:
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip("\n")
            log_file.write(line + "\n")
            log_file.flush()
            _emit(line)
    run.exit_code = await proc.wait()
    _proc = None
    run.done_event.set()


async def start_sync(
    connections_path: Path,
    *,
    full_refresh: bool = False,
    connector: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> str:
    """Launch ``flashlight ingest`` against *connections_path* and return its
    ``run_id`` as soon as the subprocess exists — not once it finishes.

    The tail (writing the log file, calling every :func:`subscribe`r) runs as a
    module-level background task from here on; the caller is free to await
    :func:`wait_for_current`, just watch via :func:`subscribe`, or do neither and
    let the sync run unattended. Raises ``RuntimeError`` if one is already running
    — this dashboard is single-user/single-sync, same assumption
    ``views/connections.py``'s "Test connection" ponytail note makes elsewhere.

    ``connector`` restricts the run to one connector (the per-connection Sync
    button); omitted, every enabled connector runs (the "Sync now" button).
    ``start``/``end`` restrict the pull window; omitted, the CLI's own default
    (a 35-day lookback) applies.
    """
    global _proc, _tail_task, _current
    if is_running():
        raise RuntimeError("A sync is already running")
    run_id = uuid.uuid4().hex
    # PYTHONUNBUFFERED=1 is load-bearing here, not cosmetic: Python fully
    # block-buffers stdout (~8KB) whenever it isn't a real terminal, which a
    # piped subprocess never is. Without it the child accumulates output
    # silently and the tail's `async for` just... waits — the tail freezes
    # after whatever fit in the last flush (often just the first line) until
    # the buffer fills or the process exits, at which point everything arrives
    # in one dump. Exactly the "stuck at 0/1 connectors done" bug this fixes.
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
    total = 1 if connector is not None else len(load_connections(str(connections_path)))
    _proc = proc
    _recent.clear()
    run = _Run(
        run_id=run_id,
        connector=connector,
        total=total,
        full_refresh=full_refresh,
        started_at=datetime.now(UTC),
    )
    _current = run
    _tail_task = asyncio.create_task(_tail(proc, run, paths.sync_log_path(run_id)))
    return run_id


async def wait_for_current() -> tuple[int, str] | None:
    """Block until the in-progress (or just-finished, unread) sync completes.

    ``None`` if nothing has run this process's lifetime. Safe to await from
    several callers/tabs at once (each just waits on the same run's event); safe
    to cancel — cancelling this await only detaches that one caller, never the
    underlying sync (see the module docstring).
    """
    run = _current
    if run is None:
        return None
    await run.done_event.wait()
    assert run.exit_code is not None
    return run.exit_code, run.run_id


async def stream_sync(
    connections_path: Path,
    on_line: Callable[[str], None],
    *,
    full_refresh: bool = False,
    connector: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> tuple[int, str]:
    """Run a sync to completion, calling ``on_line`` with each output line as it
    streams and returning ``(exit_code, run_id)`` once it finishes — the direct,
    await-to-completion contract :func:`start_sync` + :func:`subscribe` +
    :func:`wait_for_current` are built from. Kept for callers (and tests) that
    want that single call; unlike the old implementation, cancelling *this*
    coroutine no longer kills the subprocess, since the actual run lives in a
    module-level task started before this function's first real await.
    """
    run_id = await start_sync(
        connections_path,
        full_refresh=full_refresh,
        connector=connector,
        start=start,
        end=end,
    )
    unsubscribe = subscribe(on_line)
    try:
        result = await wait_for_current()
    finally:
        unsubscribe()
    assert result is not None
    exit_code, _ = result
    return exit_code, run_id
