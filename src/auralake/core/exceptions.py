"""Auralake exception hierarchy. All errors inherit from :class:`AuraLakeError`."""

from __future__ import annotations


class AuraLakeError(Exception):
    """Base exception for all auralake operations."""


class ConfigError(AuraLakeError):
    """Raised when configuration is missing, malformed, or invalid."""


class ConnectorError(AuraLakeError):
    """Raised for ingestion-connector failures (API calls, auth, parsing)."""

    def __init__(self, connector: str, message: str) -> None:
        self.connector = connector
        super().__init__(f"[{connector}] {message}")


class FocusValidationError(AuraLakeError):
    """Raised when an incoming record violates a FOCUS data-integrity rule."""


class IngestError(AuraLakeError):
    """Raised when one or more connectors failed during an ingest run.

    Per-connector failures are isolated (the others still ingest and GOLD still
    rebuilds), but the run as a whole is not a success — this carries the failed
    connector names so the CLI can report them and exit non-zero.
    """

    def __init__(self, failed: list[str]) -> None:
        self.failed = failed
        super().__init__(f"ingest failed for: {', '.join(failed)}")


class DatabaseError(AuraLakeError):
    """Raised for database connection or query failures."""
