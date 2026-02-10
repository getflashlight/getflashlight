"""Request/response schemas for connections and auth key endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class ConnectionCreate(BaseModel):
    provider: str
    name: str
    is_default: bool = False
    config: dict[str, Any] = {}
    credentials: dict[str, Any] | None = None


class ConnectionUpdate(BaseModel):
    is_default: bool | None = None
    config: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None


class ConnectionResponse(BaseModel):
    id: uuid.UUID
    provider: str
    name: str
    is_default: bool
    config: dict[str, Any]
    has_credentials: bool
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyCreate(BaseModel):
    name: str


class ApiKeyCreateResponse(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    key: str  # raw key — shown once
    created_at: datetime


class BootstrapResponse(BaseModel):
    """Returned by the one-shot /bootstrap endpoint."""

    id: uuid.UUID
    name: str
    key_prefix: str
    key: str  # raw key — shown once
    created_at: datetime


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
