"""Business-logic service for provider connections.

Sits between the repository (pure CRUD) and consumers (FastAPI router, CLI).
Handles encryption, mapping to framework-agnostic ``ConnectionInfo``, and
raises ``AuraLakeError`` subclasses instead of HTTP exceptions.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from auralake_shared.core.exceptions import AuraLakeError
from pydantic import BaseModel
from sqlmodel import Session

from auralake_backend.core.encryption import encrypt
from auralake_backend.db.connection_repository import ProviderConnectionRepository
from auralake_backend.db.models import ProviderConnection


class ConnectionNotFoundError(AuraLakeError):
    """Raised when a connection ID does not exist."""


class ConnectionInfo(BaseModel):
    """Framework-agnostic representation of a provider connection."""

    id: uuid.UUID
    provider: str
    name: str
    is_default: bool
    config: dict[str, Any]
    has_credentials: bool
    created_at: datetime
    updated_at: datetime


def _to_info(conn: ProviderConnection) -> ConnectionInfo:
    return ConnectionInfo(
        id=conn.id,
        provider=conn.provider,
        name=conn.name,
        is_default=conn.is_default,
        config=conn.config or {},
        has_credentials=conn.encrypted_credentials is not None,
        created_at=conn.created_at,
        updated_at=conn.updated_at,
    )


class ConnectionService:
    """Encapsulates all connection business logic.

    Parameters
    ----------
    session:
        An active SQLModel / SQLAlchemy ``Session``.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repo = ProviderConnectionRepository(session)

    def list_all(self) -> list[ConnectionInfo]:
        return [_to_info(c) for c in self._repo.list_all()]

    def get(self, connection_id: uuid.UUID) -> ConnectionInfo:
        conn = self._repo.get(connection_id)
        if conn is None:
            raise ConnectionNotFoundError(f"Connection {connection_id} not found")
        return _to_info(conn)

    def create(
        self,
        provider: str,
        name: str,
        is_default: bool = False,
        config: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> ConnectionInfo:
        encrypted = encrypt(json.dumps(credentials)) if credentials else None
        conn = ProviderConnection(
            provider=provider,
            name=name,
            is_default=is_default,
            config=config or {},
            encrypted_credentials=encrypted,
        )
        conn = self._repo.create(conn)
        return _to_info(conn)

    def update(
        self,
        connection_id: uuid.UUID,
        is_default: bool | None = None,
        config: dict[str, Any] | None = None,
        credentials: dict[str, Any] | None = None,
    ) -> ConnectionInfo:
        conn = self._repo.get(connection_id)
        if conn is None:
            raise ConnectionNotFoundError(f"Connection {connection_id} not found")

        if is_default is not None:
            conn.is_default = is_default
        if config is not None:
            conn.config = config
        if credentials is not None:
            conn.encrypted_credentials = encrypt(json.dumps(credentials))
        conn.updated_at = datetime.now(UTC)

        conn = self._repo.update(conn)
        return _to_info(conn)

    def delete(self, connection_id: uuid.UUID) -> None:
        if not self._repo.delete(connection_id):
            raise ConnectionNotFoundError(f"Connection {connection_id} not found")
