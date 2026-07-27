"""Flashlight exception hierarchy. All errors inherit from :class:`FlashlightError`."""

from __future__ import annotations


class FlashlightError(Exception):
    """Base exception for all flashlight operations."""


class ConfigError(FlashlightError):
    """Raised when configuration is missing, malformed, or invalid."""


class ConnectorError(FlashlightError):
    """Raised for ingestion-connector failures (API calls, auth, parsing)."""

    def __init__(self, connector: str, message: str) -> None:
        self.connector = connector
        super().__init__(f"[{connector}] {message}")


class FocusValidationError(FlashlightError):
    """Raised when an incoming record violates a FOCUS data-integrity rule."""


class IngestError(FlashlightError):
    """Raised after an ingest run if any connector failed.

    Every connector runs regardless of earlier failures — one broken source
    must not block a fresh pull from the others. GOLD is rebuilt from whatever
    succeeded, and this is raised at the end so the CLI can report the failed
    connector name(s) and exit non-zero.
    """

    def __init__(self, failed: list[str]) -> None:
        self.failed = failed
        super().__init__(f"ingest failed for: {', '.join(failed)}")
