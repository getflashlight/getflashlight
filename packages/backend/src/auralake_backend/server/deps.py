"""Shared FastAPI dependencies for the auralake server."""

from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.config import AuraLakeConfig, AutomationLevel
from fastapi import Depends, Query, Request


def get_config(request: Request) -> AuraLakeConfig:
    """Return the application configuration stored in ``app.state``."""
    return request.app.state.config


def get_context(
    request: Request,
    workspace: str | None = Query(default=None, description="Workspace scope"),
    automation_level: AutomationLevel = Query(
        default=AutomationLevel.RECOMMEND,
        description="Automation level for this request",
    ),
    dry_run: bool = Query(default=False, description="Prevent mutating operations"),
    create_pr: bool = Query(
        default=False,
        description="Submit changes as a pull request",
    ),
    config: AuraLakeConfig = Depends(get_config),
) -> ExecutionContext:
    """Build an :class:`ExecutionContext` from query parameters and app state."""
    return ExecutionContext(
        config=config,
        provider=request.app.state.provider,
        automation_level=automation_level,
        dry_run=dry_run,
        create_pr=create_pr,
        workspace=workspace,
    )
