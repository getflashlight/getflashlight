"""Request/response schemas for connections and auth key endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from auralake_shared.models.config import ConnectionProvider
from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------


class ConnectionCreate(BaseModel):
    provider: ConnectionProvider
    name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$",
        examples=["prod-workspace"],
    )
    is_default: bool = False
    config: dict[str, Any] = Field(
        default={},
        examples=[{"host": "https://myco.cloud.databricks.com", "sql_warehouse_id": "abc123"}],
    )
    credentials: dict[str, Any] | None = Field(
        default=None,
        examples=[{"token": "dapi..."}],
    )

    @field_validator("config", "credentials")
    @classmethod
    def reject_empty_keys(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            bad = [k for k in v if not k or not k.strip()]
            if bad:
                raise ValueError("Dictionary keys must be non-empty strings")
        return v


class ConnectionUpdate(BaseModel):
    is_default: bool | None = None
    config: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None

    @field_validator("config", "credentials")
    @classmethod
    def reject_empty_keys(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        if v is not None:
            bad = [k for k in v if not k or not k.strip()]
            if bad:
                raise ValueError("Dictionary keys must be non-empty strings")
        return v


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
    name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_ -]*$",
        examples=["my-api-key"],
    )


class ApiKeyCreateResponse(BaseModel):
    id: uuid.UUID
    name: str
    key: str  # raw key — shown once
    created_at: datetime


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
