"""Auralake exception hierarchy.

All auralake-specific exceptions inherit from :class:`AuraLakeError` so callers
can catch a single base class when they do not need to distinguish between
failure modes.
"""

from __future__ import annotations


class AuraLakeError(Exception):
    """Base exception for all auralake operations."""


class ConfigError(AuraLakeError):
    """Raised when configuration is missing, malformed, or invalid."""


class ProviderError(AuraLakeError):
    """Raised for provider-related failures (API calls, auth, etc.)."""

    def __init__(self, provider: str, message: str) -> None:
        self.provider = provider
        super().__init__(f"[{provider}] {message}")


class ProviderNotFoundError(ProviderError):
    """Raised when a requested provider is not registered or available."""


class AuthenticationError(ProviderError):
    """Raised when provider authentication fails."""


class APIError(ProviderError):
    """Raised when a provider API call returns an error response."""

    def __init__(
        self,
        provider: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        self.status_code = status_code
        super().__init__(provider, message)


class DatabaseError(AuraLakeError):
    """Raised for database connection or query failures."""


class AutomationError(AuraLakeError):
    """Raised for automation engine failures."""


class SafetyError(AutomationError):
    """Raised when an action exceeds configured safety rails."""


class GitIntegrationError(AuraLakeError):
    """Raised for git/GitHub integration failures."""


class DABParserError(AuraLakeError):
    """Raised when Databricks Asset Bundle parsing fails."""


class AnalyzerError(AuraLakeError):
    """Raised when a cost/usage analyzer encounters an error."""
