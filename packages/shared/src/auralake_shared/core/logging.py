"""Structured logging configuration using *structlog*.

Call :func:`setup_logging` once at application startup (typically inside the
CLI entry-point) before any logger is used.  Subsequent calls to
:func:`get_logger` return pre-configured bound loggers that include the
caller's module name as context.
"""

from __future__ import annotations

import logging

import structlog


def setup_logging(verbose: bool = False) -> None:
    """Configure structlog with console rendering.

    Parameters
    ----------
    verbose:
        When *True* the root log level is set to ``DEBUG``; otherwise
        ``INFO`` is used.
    """
    level = logging.DEBUG if verbose else logging.INFO

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger with the given *name* as context.

    Parameters
    ----------
    name:
        Typically ``__name__`` of the calling module.
    """
    return structlog.get_logger(name)
