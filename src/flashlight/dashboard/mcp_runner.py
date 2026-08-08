"""Start and stop ``flashlight mcp serve`` from the dashboard, as a subprocess.

Same reasoning as ``ingest_runner.py``: shell out to the CLI entrypoint a terminal
user would run rather than calling into ``mcp/server.py`` in-process. Here it isn't
only about keeping one code path — ``MCPServer.run(transport="streamable-http")``
blocks its thread and owns an event loop, so it *cannot* be called from inside the
dashboard's own loop. The subprocess also resolves its own settings, so a server
started from this page and one started from a terminal are the same server.

Unlike an ingest, an MCP server has no natural end: it runs until something stops it.
That forces two things ``ingest_runner`` doesn't need:

* **A module-level handle.** NiceGUI page functions re-run per client, so a process
  kept in a page closure would be unreachable from the next page load — and
  unstoppable. There's exactly one server per dashboard process, so one module global
  is the whole registry.
* **Status that isn't just "did we start it".** :func:`status` also probes the port, so
  a server someone launched in a terminal reads as up. The dashboard is a control
  surface for the server, not its owner.

The process is a child of the dashboard, so it dies with it. That's stated in the UI
rather than worked around — a detached server the dashboard can't see or stop would be
worse than one with an honest lifetime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from collections import deque
from collections.abc import Callable

from flashlight.core.logging import get_logger
from flashlight.core.settings import get_settings
from flashlight.dashboard.launch import port_in_use
from flashlight.lake import paths

logger = get_logger(__name__)

# How long `terminate()` gets to land before escalating to `kill()`. Uvicorn's graceful
# shutdown closes idle keep-alive connections immediately; a streaming MCP session can
# hold on a little longer.
_STOP_TIMEOUT = 5.0

# Recent output, kept so a page opened *after* the server started still shows context
# instead of an empty log until the next line happens to arrive.
_TAIL_LINES = 400

_proc: asyncio.subprocess.Process | None = None
_tail_task: asyncio.Task[None] | None = None
_recent: deque[str] = deque(maxlen=_TAIL_LINES)
_subscribers: list[Callable[[str], None]] = []


def endpoint() -> str:
    """The streamable-http URL an MCP client connects to."""
    settings = get_settings()
    host = "127.0.0.1" if settings.mcp_host in ("", "0.0.0.0") else settings.mcp_host
    return f"http://{host}:{settings.mcp_port}/mcp"


def is_running() -> bool:
    """True if *this* dashboard started a server that hasn't exited."""
    return _proc is not None and _proc.returncode is None


def is_listening() -> bool:
    """True if anything is serving the configured MCP port — ours or a terminal's."""
    settings = get_settings()
    return port_in_use(settings.mcp_host, settings.mcp_port)


def recent_lines() -> list[str]:
    return list(_recent)


def subscribe(on_line: Callable[[str], None]) -> Callable[[], None]:
    """Register *on_line* for every subsequent output line; returns an unsubscribe.

    A list of callbacks rather than one, because the log survives page navigation: two
    browser tabs can both be watching the same long-lived server.
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


async def _tail(proc: asyncio.subprocess.Process) -> None:
    """Pump the child's merged stdout/stderr into the log file and the subscribers."""
    assert proc.stdout is not None
    log_path = paths.mcp_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Appended, not truncated: a stop/start cycle within one dashboard session should
    # leave the earlier attempt's failure readable — that's usually the thing you need.
    with log_path.open("a") as log_file:
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="replace").rstrip("\n")
            log_file.write(line + "\n")
            log_file.flush()
            _emit(line)
    _emit(f"mcp server exited — code {await proc.wait()}")


async def start() -> bool:
    """Launch ``flashlight mcp serve``. False if one is already up (ours or not).

    Never raises for the ordinary failures — a refusal (``FLASHLIGHT_DEMO=1``), a busy
    port, a bad setting — those arrive as output lines and an exit code, which is what
    the page shows. Only a genuine spawn failure propagates.
    """
    global _proc, _tail_task
    if is_running() or is_listening():
        return False
    # PYTHONUNBUFFERED=1 is load-bearing, not cosmetic — see ingest_runner.stream_sync:
    # a piped child block-buffers stdout, so without it the live tail shows nothing until
    # the buffer fills, which for a server that logs one startup banner is ~forever.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    cmd = [sys.executable, "-m", "flashlight.cli", "mcp", "serve"]
    _proc = await asyncio.create_subprocess_exec(
        *cmd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    _tail_task = asyncio.create_task(_tail(_proc))
    logger.info("mcp_server_started", pid=_proc.pid, endpoint=endpoint())
    _emit(f"starting mcp server (pid {_proc.pid}) on {endpoint()}")
    return True


async def stop() -> bool:
    """Terminate the server we started, escalating to kill. False if we started none.

    Returns False for a server started in a terminal too — we can see it on the port
    but have no handle on it, and guessing at a PID to signal would be worse than
    telling the user to stop it where they started it.
    """
    global _proc, _tail_task
    proc, _proc = _proc, None
    if proc is None or proc.returncode is not None:
        return False
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_STOP_TIMEOUT)
    except TimeoutError:
        _emit(f"mcp server ignored terminate after {_STOP_TIMEOUT:.0f}s — killing it")
        proc.kill()
        await proc.wait()
    if _tail_task is not None:
        # The tail's `async for` ends on EOF once the child is gone; awaiting it flushes
        # the last lines (including the exit-code line) before the caller re-renders.
        with contextlib.suppress(asyncio.CancelledError):
            await _tail_task
        _tail_task = None
    logger.info("mcp_server_stopped", pid=proc.pid)
    return True
