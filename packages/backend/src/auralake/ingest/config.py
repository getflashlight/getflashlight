"""Connector configuration loaded from a YAML connections file.

Credentials are read from environment variables referenced by ``*_env`` fields
rather than stored in the file. Single-tenant, self-hosted: one connections file
per deployment.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from auralake.core.exceptions import ConfigError
from auralake.lake import paths


class AwsFocusConfig(BaseModel):
    type: str = "aws_focus"
    enabled: bool = True
    s3_bucket: str
    s3_prefix: str = ""
    region: str = "us-east-1"
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    # Optional allow-list of FOCUS ServiceName values to ingest. AWS Data Exports
    # is account-wide and cannot be scoped per service at the source, so a
    # data-platform deployment narrows here instead. Empty list = ingest every
    # service (the full account).
    include_services: list[str] = Field(default_factory=list)

    @field_validator("s3_prefix")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        # AWS Data Exports inserts its own ``/`` separator before the export name,
        # so a trailing slash here yields a ``focus_data//export-name`` double
        # slash in the delivered keys. Normalize any accidental trailing slash(es).
        return v.rstrip("/")


class FocusFileConfig(BaseModel):
    type: str = "focus_file"
    enabled: bool = True
    path: str  # local path to a FOCUS CSV or Parquet file
    # Sample/backfill files often predate the ingest window — ingest all rows by
    # default rather than filtering by the run's date range.
    respect_window: bool = False


class DatabricksConfig(BaseModel):
    type: str = "databricks"
    enabled: bool = True
    host: str
    token_env: str = "DATABRICKS_TOKEN"
    sql_warehouse_id: str | None = None


class AwsInfraConfig(BaseModel):
    type: str = "aws_infra"
    enabled: bool = False
    region: str = "us-east-1"
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"
    cluster_tag_key: str = "ClusterId"
    tag_filters: dict[str, str] = Field(default_factory=lambda: {"Vendor": "Databricks"})


_CONFIG_TYPES: dict[str, type[BaseModel]] = {
    "aws_focus": AwsFocusConfig,
    "focus_file": FocusFileConfig,
    "databricks": DatabricksConfig,
    "aws_infra": AwsInfraConfig,
}


class ConnectionsFile(BaseModel):
    connectors: list[
        AwsFocusConfig | FocusFileConfig | DatabricksConfig | AwsInfraConfig
    ] = Field(default_factory=list)


def env(name: str) -> str | None:
    """Read an environment variable (helper for connectors)."""
    return os.environ.get(name)


def load_connections(path: str | None = None) -> list[BaseModel]:
    """Parse the connections YAML into typed, enabled connector configs.

    Defaults to ``<home>/config/connections.yml`` (what ``auralake init`` writes).
    """
    cfg_path = Path(path) if path else paths.connections_path()
    if not cfg_path.exists():
        raise ConfigError(
            f"Connections file not found: {cfg_path}. Run `auralake init` first."
        )

    raw = yaml.safe_load(cfg_path.read_text()) or {}
    entries = raw.get("connectors", [])
    if not isinstance(entries, list):
        raise ConfigError("`connectors` must be a list")

    configs: list[BaseModel] = []
    for entry in entries:
        ctype = entry.get("type")
        model = _CONFIG_TYPES.get(ctype)
        if model is None:
            raise ConfigError(f"Unknown connector type: {ctype!r}")
        cfg = model.model_validate(entry)
        if getattr(cfg, "enabled", True):
            configs.append(cfg)
    return configs
