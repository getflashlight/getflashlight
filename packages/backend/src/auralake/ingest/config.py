"""Connector configuration loaded from a YAML connections file.

Credentials are read from environment variables referenced by ``*_env`` fields
rather than stored in the file. Single-tenant, self-hosted: one connections file
per deployment.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from auralake.core.exceptions import ConfigError
from auralake.core.settings import get_settings


class AwsFocusConfig(BaseModel):
    type: str = "aws_focus"
    enabled: bool = True
    s3_bucket: str
    s3_prefix: str = ""
    region: str = "us-east-1"
    access_key_env: str = "AWS_ACCESS_KEY_ID"
    secret_key_env: str = "AWS_SECRET_ACCESS_KEY"


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
    "databricks": DatabricksConfig,
    "aws_infra": AwsInfraConfig,
}


class ConnectionsFile(BaseModel):
    connectors: list[AwsFocusConfig | DatabricksConfig | AwsInfraConfig] = Field(
        default_factory=list
    )


def env(name: str) -> str | None:
    """Read an environment variable (helper for connectors)."""
    return os.environ.get(name)


def load_connections(path: str | None = None) -> list[BaseModel]:
    """Parse the connections YAML into typed, enabled connector configs."""
    cfg_path = Path(path or get_settings().connections_path)
    if not cfg_path.exists():
        raise ConfigError(f"Connections file not found: {cfg_path}")

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
