"""Authentication helpers for Databricks SDK and AWS (boto3)."""
from __future__ import annotations

from databricks.sdk import WorkspaceClient

from auralake.core.exceptions import AuthenticationError
from auralake.models.config import DatabricksConfig, DatabricksWorkspaceConfig


def get_workspace_client(
    config: DatabricksConfig, workspace_name: str | None = None
) -> WorkspaceClient:
    """Get a Databricks WorkspaceClient for the given workspace."""
    ws_config = _resolve_workspace(config, workspace_name)
    try:
        return WorkspaceClient(host=ws_config.host)
    except Exception as exc:
        raise AuthenticationError(
            "databricks", f"Failed to authenticate: {exc}"
        ) from exc


def _resolve_workspace(
    config: DatabricksConfig, name: str | None
) -> DatabricksWorkspaceConfig:
    if name and name in config.workspaces:
        return config.workspaces[name]
    for ws_name, ws_config in config.workspaces.items():
        if ws_config.is_default:
            return ws_config
    if config.workspaces:
        return next(iter(config.workspaces.values()))
    raise AuthenticationError("databricks", "No workspaces configured")


def get_boto3_session(region: str | None = None):
    """Get a boto3 session for AWS API calls."""
    import boto3

    return boto3.Session(region_name=region)
