"""Authentication helpers for Databricks SDK and AWS (boto3)."""

from __future__ import annotations

from typing import Any

from auralake_shared.core.exceptions import AuthenticationError
from auralake_shared.models.config import (
    DatabricksAWSConfig,
    DatabricksConfig,
    DatabricksWorkspaceConfig,
)
from databricks.sdk import WorkspaceClient
from databricks.sdk.config import Config


def get_workspace_client(
    config: DatabricksConfig, workspace_name: str | None = None
) -> WorkspaceClient:
    """Get a Databricks WorkspaceClient for the given workspace."""
    ws_config = _resolve_workspace(config, workspace_name)
    try:
        cfg_kwargs: dict[str, Any] = {
            "host": ws_config.host,
            "http_timeout_seconds": 30,
            "retry_timeout_seconds": 30,
        }
        if ws_config.token:
            cfg_kwargs["token"] = ws_config.token
        if ws_config.client_id:
            cfg_kwargs["client_id"] = ws_config.client_id
        if ws_config.client_secret:
            cfg_kwargs["client_secret"] = ws_config.client_secret
        return WorkspaceClient(config=Config(**cfg_kwargs))
    except Exception as exc:
        raise AuthenticationError("databricks", f"Failed to authenticate: {exc}") from exc


def _resolve_workspace(config: DatabricksConfig, name: str | None) -> DatabricksWorkspaceConfig:
    if name and name in config.workspaces:
        return config.workspaces[name]
    for ws_name, ws_config in config.workspaces.items():
        if ws_config.is_default:
            return ws_config
    if config.workspaces:
        return next(iter(config.workspaces.values()))
    raise AuthenticationError("databricks", "No workspaces configured")


def get_boto3_session(
    region: str | None = None,
    aws_config: DatabricksAWSConfig | None = None,
) -> object:
    """Get a boto3 session for AWS API calls."""
    import boto3  # type: ignore[import-untyped]

    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    if aws_config:
        if not region:
            kwargs["region_name"] = aws_config.region
        if aws_config.access_key_id:
            kwargs["aws_access_key_id"] = aws_config.access_key_id
        if aws_config.secret_access_key:
            kwargs["aws_secret_access_key"] = aws_config.secret_access_key
        if aws_config.session_token:
            kwargs["aws_session_token"] = aws_config.session_token

    return boto3.Session(**kwargs)
