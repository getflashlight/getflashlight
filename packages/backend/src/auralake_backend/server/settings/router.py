"""Connections and auth key management endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from typing import Any

import structlog
from auralake_shared.providers import get_provider
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select
from starlette.responses import Response

from auralake_backend.core.config_loader import is_configured, load_config_from_db
from auralake_backend.db.connection_service import ConnectionNotFoundError, ConnectionService
from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import ApiKey
from auralake_backend.server.auth import create_api_key, require_auth
from auralake_backend.server.settings.schemas import (
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyResponse,
    ConnectionCreate,
    ConnectionResponse,
    ConnectionUpdate,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Session dependency
# ---------------------------------------------------------------------------


def _get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
        yield session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reload_app_state(app: Any, session: Session) -> None:
    """Reload config and provider after a connection mutation."""
    config = load_config_from_db(session)
    app.state.config = config
    app.state.configured = is_configured(session)
    if app.state.configured:
        try:
            app.state.provider = get_provider(config.provider, config)
        except Exception:
            logger.warning("provider_reinit_failed", provider=config.provider)
            app.state.provider = None
    else:
        app.state.provider = None


# ---------------------------------------------------------------------------
# Connection endpoints
# ---------------------------------------------------------------------------


@router.get("/connections", response_model=list[ConnectionResponse])
async def list_connections(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> list[ConnectionResponse]:
    svc = ConnectionService(session)
    return [ConnectionResponse.model_validate(c, from_attributes=True) for c in svc.list_all()]


@router.post("/connections", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    body: ConnectionCreate,
    request: Request,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> ConnectionResponse:
    svc = ConnectionService(session)
    info = svc.create(
        provider=body.provider,
        name=body.name,
        is_default=body.is_default,
        config=body.config,
        credentials=body.credentials,
    )
    _reload_app_state(request.app, session)
    return ConnectionResponse.model_validate(info, from_attributes=True)


@router.get("/connections/{connection_id}", response_model=ConnectionResponse)
async def get_connection(
    connection_id: uuid.UUID,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> ConnectionResponse:
    svc = ConnectionService(session)
    try:
        info = svc.get(connection_id)
    except ConnectionNotFoundError:
        raise HTTPException(status_code=404, detail="Connection not found")
    return ConnectionResponse.model_validate(info, from_attributes=True)


@router.put("/connections/{connection_id}", response_model=ConnectionResponse)
async def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionUpdate,
    request: Request,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> ConnectionResponse:
    svc = ConnectionService(session)
    try:
        info = svc.update(
            connection_id,
            is_default=body.is_default,
            config=body.config,
            credentials=body.credentials,
        )
    except ConnectionNotFoundError:
        raise HTTPException(status_code=404, detail="Connection not found")
    _reload_app_state(request.app, session)
    return ConnectionResponse.model_validate(info, from_attributes=True)


@router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: uuid.UUID,
    request: Request,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> Response:
    svc = ConnectionService(session)
    try:
        svc.delete(connection_id)
    except ConnectionNotFoundError:
        raise HTTPException(status_code=404, detail="Connection not found")
    _reload_app_state(request.app, session)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Auth key endpoints
# ---------------------------------------------------------------------------


@router.post("/auth/keys", response_model=ApiKeyCreateResponse, status_code=201)
async def create_key(
    body: ApiKeyCreate,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> ApiKeyCreateResponse:
    record, raw_key = create_api_key(session, body.name)
    return ApiKeyCreateResponse(
        id=record.id,
        name=record.name,
        key_prefix=record.key_prefix,
        key=raw_key,
        created_at=record.created_at,
    )


@router.get("/auth/keys", response_model=list[ApiKeyResponse])
async def list_keys(
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> list[ApiKeyResponse]:
    keys = list(session.exec(select(ApiKey).where(ApiKey.is_active == True)).all())  # noqa: E712
    return [
        ApiKeyResponse(
            id=k.id,
            name=k.name,
            key_prefix=k.key_prefix,
            is_active=k.is_active,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.delete("/auth/keys/{key_id}", status_code=204)
async def revoke_key(
    key_id: uuid.UUID,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> Response:
    key = session.get(ApiKey, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    key.is_active = False
    session.add(key)
    session.commit()
    return Response(status_code=204)
