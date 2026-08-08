"""setup_logging must not leak raw ANSI escapes into captured (non-tty) output —
regression test for the dashboard's sync-output dialog rendering garbled boxes
where colored log lines used to be (ConsoleRenderer defaults colors on
unconditionally; see core/logging.py).
"""

from __future__ import annotations

import sys

from flashlight.core.logging import get_logger, setup_logging


def test_no_ansi_escapes_when_stdout_is_not_a_tty(capsys) -> None:  # type: ignore[no-untyped-def]
    setup_logging()
    get_logger("test").info("hello")
    assert "\x1b" not in capsys.readouterr().out


def test_ansi_escapes_present_when_stdout_is_a_tty(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    setup_logging()
    get_logger("test").info("hello")
    assert "\x1b" in capsys.readouterr().out
