"""stream_sync — the dashboard's live-tail sync (see dashboard/views/connections.py).

Streams a subprocess's stdout line-by-line via a callback, instead of the old
blocking subprocess.run + one big dump at the end (users watching a sync had no
feedback for however long it ran). asyncio.create_subprocess_exec is mocked —
no real subprocess needed to test the streaming/callback/exit-code contract.
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from flashlight.core.settings import get_settings
from flashlight.dashboard.ingest_runner import is_running, start_sync, stream_sync, wait_for_current
from flashlight.lake import paths


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    # stream_sync now writes the tailed transcript to a file under FLASHLIGHT_HOME
    # (see lake/paths.py::sync_log_path) — sandbox it like every other lake test,
    # autouse so nothing here has to remember to opt in.
    monkeypatch.setenv("FLASHLIGHT_HOME", str(tmp_path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self) -> _FakeStdout:
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProcess:
    def __init__(self, lines: list[bytes], returncode: int) -> None:
        self.stdout = _FakeStdout(lines)
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


def test_stream_sync_streams_lines_and_returns_exit_code(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    connections_path = tmp_path / "connections.yml"
    connections_path.write_text("connectors: []\n")

    captured: dict[str, Any] = {}

    async def _fake_create_subprocess_exec(*cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        return _FakeProcess([b"line one\n", b"line two\n"], returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    seen: list[str] = []
    returncode, run_id = asyncio.run(
        stream_sync(connections_path, seen.append, full_refresh=True, connector="redshift")
    )

    assert seen == ["line one", "line two"]
    assert returncode == 1
    assert "--full-refresh" in captured["cmd"]
    assert captured["cmd"][-2:] == ("--connector", "redshift")
    assert "--run-id" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--run-id") + 1] == run_id
    # Load-bearing, not cosmetic: without it the child's stdout is fully
    # block-buffered (it's a pipe, not a tty) and the live tail freezes until
    # the buffer fills or the process exits — see stream_sync's own docstring.
    assert captured["env"]["PYTHONUNBUFFERED"] == "1"
    # The tailed transcript is also written to disk, line by line, as it
    # streams — not just handed to the on_line callback — so "Recent sync
    # history" can link back to it after the live dialog is closed.
    assert paths.sync_log_path(run_id).read_text() == "line one\nline two\n"


def test_stream_sync_passes_start_and_end_through_to_the_cli(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    connections_path = tmp_path / "connections.yml"
    connections_path.write_text("connectors: []\n")

    captured: dict[str, Any] = {}

    async def _fake_create_subprocess_exec(*cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return _FakeProcess([], returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    asyncio.run(
        stream_sync(
            connections_path,
            lambda _line: None,
            start=date(2026, 5, 1),
            end=date(2026, 7, 31),
        )
    )

    assert "--start" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--start") + 1] == "2026-05-01"
    assert "--end" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--end") + 1] == "2026-07-31"


def test_start_sync_tracks_the_run_without_reading_process_returncode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The live dashboard state comes from its _Run, not a subprocess implementation detail."""
    connections_path = tmp_path / "connections.yml"
    connections_path.write_text("connectors: []\n")

    async def _fake_create_subprocess_exec(*_cmd, **_kwargs):  # type: ignore[no-untyped-def]
        return _FakeProcess([], returncode=0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    async def _run() -> None:
        await start_sync(connections_path)
        assert is_running()
        assert await wait_for_current() is not None
        assert not is_running()

    asyncio.run(_run())
