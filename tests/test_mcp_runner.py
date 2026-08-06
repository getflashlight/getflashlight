"""Start/stop lifecycle for the MCP server the dashboard can launch.

Every test drives a stub child (a short ``python -c``), never the real
``flashlight mcp serve``: what's under test is the process handling — the module-level
handle, the tail, the terminate/kill escalation — not the server, which
``tests/test_demo_gate.py`` and the MCP tests already cover.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import Iterator

import pytest

from flashlight.core.settings import get_settings
from flashlight.dashboard import mcp_runner
from flashlight.lake import paths


@pytest.fixture(autouse=True)
def clean_runner(tmp_path, monkeypatch) -> Iterator[None]:  # type: ignore[no-untyped-def]
    """The runner's handle is module-level by design (NiceGUI page functions re-run per
    client), so it has to be reset between tests or one test's child leaks into the next."""
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    # Off the default 8002: start() declines when the port is already served, so a real
    # server (or anything else) on a developer's 8002 would fail these tests for a reason
    # that has nothing to do with the code.
    monkeypatch.setenv("FLASHLIGHT_MCP_PORT", "58002")
    get_settings.cache_clear()
    mcp_runner._proc = None  # noqa: SLF001
    mcp_runner._tail_task = None  # noqa: SLF001
    mcp_runner._recent.clear()  # noqa: SLF001
    mcp_runner._subscribers.clear()  # noqa: SLF001
    yield
    # Each test stops its own child inside its own event loop; awaiting stop() here would
    # touch a transport bound to a loop that asyncio.run already closed. This is only the
    # backstop for a test that failed before its stop, so it's a bare signal.
    proc = mcp_runner._proc  # noqa: SLF001
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(Exception):
            proc.kill()
    mcp_runner._proc = None  # noqa: SLF001
    mcp_runner._tail_task = None  # noqa: SLF001
    get_settings.cache_clear()


def _stub(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Replace the child command with an inline Python program."""
    real = asyncio.create_subprocess_exec

    async def fake(*_cmd: str, **kwargs: object):  # type: ignore[no-untyped-def]
        return await real(sys.executable, "-u", "-c", body, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


# A child that prints a banner then blocks forever — the shape of a real server.
_SERVER = "print('listening', flush=True)\nimport time\nwhile True: time.sleep(0.05)"


async def _await_line(needle: str, *, tries: int = 150) -> bool:
    """Wait for *needle* to reach the subscribers. The child is a real process, so
    "started" doesn't mean "has run any code yet" — a test that stops it before it has
    installed its signal handler is testing the wrong thing."""
    for _ in range(tries):
        await asyncio.sleep(0.02)
        if any(needle in line for line in mcp_runner.recent_lines()):
            return True
    return False


def test_start_tails_output_and_stop_terminates(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub(monkeypatch, _SERVER)
    seen: list[str] = []

    async def _run() -> None:
        mcp_runner.subscribe(seen.append)
        assert await mcp_runner.start() is True
        assert mcp_runner.is_running()
        assert await _await_line("listening")
        assert any("listening" in line for line in seen)
        assert await mcp_runner.stop() is True
        assert not mcp_runner.is_running()

    asyncio.run(_run())
    # The transcript is on disk, so a failed start is still readable after the dialog
    # that showed it is gone.
    assert "listening" in paths.mcp_log_path().read_text()


def test_double_start_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """The page's Start button is clickable until a re-render lands; a second click must
    not orphan the first child (which would leave an unstoppable process on the port)."""
    _stub(monkeypatch, _SERVER)

    async def _run() -> None:
        assert await mcp_runner.start() is True
        first = mcp_runner._proc  # noqa: SLF001
        assert await mcp_runner.start() is False
        assert mcp_runner._proc is first  # noqa: SLF001
        await mcp_runner.stop()

    asyncio.run(_run())


def test_stop_without_a_start_does_not_raise() -> None:
    """Stop is also reachable right after a crash, when the handle is already gone."""
    assert asyncio.run(mcp_runner.stop()) is False


def test_stop_kills_a_child_that_ignores_terminate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A server wedged in a streaming session must not hang the dashboard's event loop
    forever — terminate escalates to kill on a timeout."""
    _stub(
        monkeypatch,
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ignoring sigterm', flush=True)\n"
        "while True: time.sleep(0.05)",
    )
    monkeypatch.setattr(mcp_runner, "_STOP_TIMEOUT", 0.3)

    async def _run() -> None:
        assert await mcp_runner.start() is True
        # Not until the child says so: SIGTERM sent before it installs SIG_IGN would just
        # kill it, and the test would pass without exercising the escalation at all.
        assert await _await_line("ignoring sigterm")
        assert await mcp_runner.stop() is True
        assert not mcp_runner.is_running()

    asyncio.run(_run())
    assert any("killing it" in line for line in mcp_runner.recent_lines())


def test_a_failed_start_is_reported_as_output_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mcp serve` exits with a message when it refuses (FLASHLIGHT_DEMO=1) or the port is
    taken. That belongs in the page's log, not as a traceback."""
    _stub(monkeypatch, "import sys; print('Refusing to start', file=sys.stderr); sys.exit(1)")

    async def _run() -> None:
        assert await mcp_runner.start() is True  # the spawn itself succeeded
        assert await _await_line("exited")

    asyncio.run(_run())
    lines = mcp_runner.recent_lines()
    assert any("Refusing to start" in line for line in lines)
    assert any("exited — code 1" in line for line in lines)
    assert not mcp_runner.is_running()


def test_endpoint_reports_a_connectable_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp_host defaults to 0.0.0.0, which is a bind address, not somewhere a client can
    dial — the copy-paste URL on the page has to be reachable."""
    monkeypatch.setenv("FLASHLIGHT_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("FLASHLIGHT_MCP_PORT", "8002")
    get_settings.cache_clear()
    assert mcp_runner.endpoint() == "http://127.0.0.1:8002/mcp"


def test_the_page_shows_the_endpoint_tools_and_the_no_auth_warning(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The page's job is to answer "is it up, what do I point at it, and what does it
    expose" — including that the port has no auth, which is the one thing a user has to
    know before binding it anywhere but localhost."""
    from nicegui import ui
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    monkeypatch.delenv("FLASHLIGHT_DEMO", raising=False)
    get_settings.cache_clear()

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/mcp-server")
            await user.should_see("Stopped")
            await user.should_see("http://127.0.0.1:58002/mcp")
            await user.should_see("No authentication")
            await user.should_see(marker="mcp-start")
            # Read from mcp.list_tools(), so this can't drift from what the server serves.
            # ui.table rows are data, not text nodes should_see can match.
            rows = " ".join(str(t.rows) for t in user.find(kind=ui.table).elements)
            assert "query_metric" in rows
            assert "run_sql" in rows

    asyncio.run(_check())


def test_the_buttons_start_and_stop_the_server_and_the_status_follows(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Click-through, because a handler with the wrong signature or a refresh that never
    fires renders identically to a working one. The runner is stubbed — this is about the
    page's wiring, not the subprocess (covered above)."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    monkeypatch.delenv("FLASHLIGHT_DEMO", raising=False)
    get_settings.cache_clear()

    running = False

    async def fake_start() -> bool:
        nonlocal running
        running = True
        return True

    async def fake_stop() -> bool:
        nonlocal running
        running = False
        return True

    monkeypatch.setattr(mcp_runner, "start", fake_start)
    monkeypatch.setattr(mcp_runner, "stop", fake_stop)
    monkeypatch.setattr(mcp_runner, "is_running", lambda: running)
    monkeypatch.setattr(mcp_runner, "is_listening", lambda: running)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/mcp-server")
            await user.should_see("Stopped")
            user.find(marker="mcp-start").click()
            await user.should_see("Running")
            # Started by us, so the page offers Stop — and says the server dies with it.
            await user.should_see(marker="mcp-stop")
            await user.should_see("exits when the dashboard does")
            user.find(marker="mcp-stop").click()
            await user.should_see("Stopped")
            await user.should_see(marker="mcp-start")

    asyncio.run(_check())


def test_the_page_says_it_cannot_stop_a_server_it_did_not_start(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A server on the port that we hold no handle on: status must still read Running, and
    the page must not offer a Stop button it can't honour."""
    from nicegui.testing.user_simulation import user_simulation

    from flashlight.dashboard.router import build_pages

    monkeypatch.delenv("FLASHLIGHT_DEMO", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(mcp_runner, "is_running", lambda: False)
    monkeypatch.setattr(mcp_runner, "is_listening", lambda: True)

    async def _check() -> None:
        async with user_simulation() as user:
            build_pages()
            await user.open("/mcp-server")
            await user.should_see("Running")
            await user.should_see("this page can't")
            await user.should_not_see(marker="mcp-stop")

    asyncio.run(_check())


def test_a_torn_down_subscriber_is_dropped_rather_than_breaking_the_others() -> None:
    """A server outlives the browser tabs watching it, so a dead callback is normal."""
    survivors: list[str] = []

    def dead(_line: str) -> None:
        raise RuntimeError("client is gone")

    mcp_runner.subscribe(dead)
    mcp_runner.subscribe(survivors.append)
    mcp_runner._emit("first")  # noqa: SLF001
    mcp_runner._emit("second")  # noqa: SLF001
    assert survivors == ["first", "second"]
    assert dead not in mcp_runner._subscribers  # noqa: SLF001
