"""Shared FastAPI dependencies for the auralake server."""

from __future__ import annotations

from collections.abc import Generator

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.config import AuraLakeConfig, AutomationLevel
from fastapi import Depends, HTTPException, Query, Request
from sqlmodel import Session

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import ApiKey
from auralake_backend.server.auth import require_auth


def get_db_session() -> Generator[Session, None, None]:
    """Yield a SQLModel session."""
    with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
        yield session


def get_config(request: Request) -> AuraLakeConfig:
    """Return the application configuration stored in ``app.state``."""
    config: AuraLakeConfig = request.app.state.config
    return config


def require_configured(request: Request) -> None:
    """Raise 503 if no provider connections have been configured yet.

    Also raises 503 when a connection exists but the provider failed to
    initialise (``app.state.provider is None``).
    """
    if not getattr(request.app.state, "configured", False):
        raise HTTPException(
            status_code=503,
            detail="Auralake is not configured. Create a provider connection first.",
        )
    if getattr(request.app.state, "provider", None) is None:
        raise HTTPException(
            status_code=503,
            detail="Provider failed to initialise. Check connection credentials.",
        )


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
    _auth: ApiKey = Depends(require_auth),
    _configured: None = Depends(require_configured),
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
