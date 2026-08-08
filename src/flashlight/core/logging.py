"""Structured logging via structlog. Call :func:`setup_logging` once at startup."""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(verbose: bool = False) -> None:
    """Configure structlog with console rendering.

    Colors only when stdout is a real terminal — ``ConsoleRenderer`` defaults to
    always-on, which dumps raw ANSI escape codes into anything that captures
    output as text (a redirected file, or the dashboard's ``subprocess.run(...,
    capture_output=True)`` for its per-connection/sync-all buttons), rendering as
    garbled boxes wherever that text is displayed instead of a plain log line.
    """
    level = logging.DEBUG if verbose else logging.INFO
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger with *name* (typically ``__name__``) as context."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
