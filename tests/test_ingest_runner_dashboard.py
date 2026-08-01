"""stream_sync — the dashboard's live-tail sync (see dashboard/views/connections.py).

Streams a subprocess's stdout line-by-line via a callback, instead of the old
blocking subprocess.run + one big dump at the end (users watching a sync had no
feedback for however long it ran). asyncio.create_subprocess_exec is mocked —
no real subprocess needed to test the streaming/callback/exit-code contract.
"""

from __future__ import annotations

import asyncio

from flashlight.dashboard import ingest_runner
from flashlight.dashboard.ingest_runner import stream_sync


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

    captured: dict[str, tuple[str, ...]] = {}

    async def _fake_create_subprocess_exec(*cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured["cmd"] = cmd
        return _FakeProcess([b"line one\n", b"line two\n"], returncode=1)

    monkeypatch.setattr(ingest_runner.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    seen: list[str] = []
    returncode = asyncio.run(
        stream_sync(connections_path, seen.append, full_refresh=True, connector="redshift")
    )

    assert seen == ["line one", "line two"]
    assert returncode == 1
    assert "--full-refresh" in captured["cmd"]
    assert captured["cmd"][-2:] == ("--connector", "redshift")
