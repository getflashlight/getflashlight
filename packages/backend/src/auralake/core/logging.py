"""Structured logging via structlog. Call :func:`setup_logging` once at startup."""

from __future__ import annotations

import logging

import structlog


def setup_logging(verbose: bool = False) -> None:
    """Configure structlog with console rendering."""
    level = logging.DEBUG if verbose else logging.INFO
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger with *name* (typically ``__name__``) as context."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
