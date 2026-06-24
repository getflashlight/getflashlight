"""Process-level settings sourced from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings shared by every entry point.

    All credentials for source connectors live in connection config files /
    env, never here — this is only the platform's own plumbing.
    """

    model_config = SettingsConfigDict(env_prefix="AURALAKE_", extra="ignore")

    database_url: str = "postgresql+psycopg://auralake:auralake@localhost:5432/auralake"
    api_key: str | None = None
    base_currency: str = "USD"
    connections_path: str = "connections.yml"
    # Off by default: migrations are applied by the dedicated `migrate` step
    # (the compose init service, or `python -m auralake.store.migrate` locally),
    # not on app startup. Set true to let `serve` self-apply on boot instead.
    auto_migrate: bool = False

    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8002


@lru_cache
def get_settings() -> Settings:
    """Return cached process settings."""
    return Settings()
