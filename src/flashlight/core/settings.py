"""Process-level settings sourced from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven settings shared by every entry point.

    All credentials for source connectors live in connection config files /
    env, never here — this is only the platform's own plumbing.

    Values come from ``os.environ`` (``FLASHLIGHT_*``). The CLI calls
    ``dotenv.load_dotenv()`` at startup so a ``.env`` in the working directory also
    feeds these — shell env still wins (``load_dotenv`` does not override).
    """

    model_config = SettingsConfigDict(env_prefix="FLASHLIGHT_", extra="ignore")

    # Root of the on-disk Parquet lake. None → the platform user-data dir
    # (see flashlight.lake.paths.home). All three processes resolve the same files
    # from here, so ingest (writer) and the MCP/dashboard readers always agree.
    home: str | None = None
    api_key: str | None = None
    base_currency: str = "USD"
    # Connector config. Relative/unset resolves under <home>/config/connections.yml.
    connections_path: str = "connections.yml"

    # Parquet write codec, applied to every COPY (BRONZE partitions + GOLD files).
    # zstd beats the snappy default on ratio at negligible read cost; DuckDB reads
    # it transparently, so only the writers care.
    parquet_compression: str = "zstd"
    parquet_compression_level: int = 3  # zstd 1–22; 3 is the ratio/speed sweet spot

    # Bounded thread-pool size for concurrent connector pulls (ingest/runner.py) —
    # caps how many connectors' fetches (and, for the vectorized ones, in-memory
    # DuckDB materializations) run at once. Small on purpose: this bounds worst-case
    # concurrent memory, not just wall-clock.
    ingest_max_workers: int = 3

    # Per-connection DuckDB memory ceiling (lake/duck.py). DuckDB's own default is
    # ~80% of system RAM, which on a laptop means one transform can starve the rest
    # of the machine — and ingest_max_workers connections can be live at once. Over
    # the cap DuckDB spills to <home>/tmp/duckdb instead of failing.
    duckdb_memory_limit: str = "4GB"

    # How far back an ingest pulls when no explicit --start/--end is given
    # (ingest/runner.py). Raise it to backfill history — forecasting needs 60+ days
    # of complete days before it will emit a trend.
    ingest_lookback_days: int = 35

    mcp_host: str = "0.0.0.0"
    mcp_port: int = 8002
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8501

    # Public-demo mode: disables the Chat and Connections pages (BYOK keychain
    # writes, outbound LLM calls, connections.yml edits, subprocess ingest) — the
    # dashboard's only write/mutation surfaces — so a self-hosted demo over mocked
    # data is safe to expose publicly. See demo/README.md.
    demo: bool = False
    # Absolute path to a prebuilt static site (e.g. `mkdocs build`'s output) to
    # mount at /docs. None (default) skips the mount and the nav entry.
    docs_dir: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Return cached process settings."""
    return Settings()
