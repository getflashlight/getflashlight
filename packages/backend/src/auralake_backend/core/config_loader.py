"""Load AuraLakeConfig from database provider_connections + env vars."""

from __future__ import annotations

import json
import os
from typing import Any

from auralake_shared.models.config import AuraLakeConfig
from sqlmodel import Session, select

from auralake_backend.core.encryption import decrypt
from auralake_backend.db.models import ProviderConnection


def load_config_from_db(session: Session) -> AuraLakeConfig:
    """Build an AuraLakeConfig from provider_connections rows and env vars.

    Priority: env vars override DB values; everything else uses Pydantic defaults.
    """
    data: dict[str, Any] = {}

    # Env var overrides for top-level fields
    db_url = os.environ.get("AURALAKE_DATABASE_URL")
    if db_url:
        data["database"] = {"url": db_url}

    provider = os.environ.get("AURALAKE_PROVIDER", "databricks")
    data["provider"] = provider

    # Read all connections
    connections = list(session.exec(select(ProviderConnection)).all())

    workspaces: dict[str, Any] = {}
    github_data: dict[str, Any] = {}
    aws_data: dict[str, Any] = {}

    for conn in connections:
        creds = _decrypt_credentials(conn.encrypted_credentials)
        cfg = dict(conn.config) if conn.config else {}

        if conn.provider == "databricks":
            ws_cfg: dict[str, Any] = {**cfg}
            ws_cfg["is_default"] = conn.is_default
            if "token" in creds:
                ws_cfg["token"] = creds["token"]
            if "client_id" in creds:
                ws_cfg["client_id"] = creds["client_id"]
            if "client_secret" in creds:
                ws_cfg["client_secret"] = creds["client_secret"]
            workspaces[conn.name] = ws_cfg

        elif conn.provider == "github":
            github_data = {**cfg}
            if "token" in creds:
                github_data["token"] = creds["token"]

        elif conn.provider == "aws":
            aws_data = {**cfg}
            if "access_key_id" in creds:
                aws_data["access_key_id"] = creds["access_key_id"]
            if "secret_access_key" in creds:
                aws_data["secret_access_key"] = creds["secret_access_key"]
            if "session_token" in creds:
                aws_data["session_token"] = creds["session_token"]

    if workspaces or aws_data:
        db_section: dict[str, Any] = {}
        if workspaces:
            db_section["workspaces"] = workspaces
        if aws_data:
            db_section["aws"] = aws_data
        data["databricks"] = db_section

    if github_data:
        data["github"] = github_data

    try:
        return AuraLakeConfig.model_validate(data)
    except Exception as exc:
        import structlog

        structlog.get_logger(__name__).warning(
            "config_validation_failed",
            error=str(exc),
        )
        # Return a minimal config so the server stays up
        return AuraLakeConfig.model_validate(
            {"database": data.get("database", {}), "provider": data.get("provider", "databricks")}
        )


def is_configured(session: Session) -> bool:
    """Return True if at least one provider connection exists."""
    stmt = select(ProviderConnection.id).limit(1)
    return session.exec(stmt).first() is not None


def _decrypt_credentials(encrypted: str | None) -> dict[str, Any]:
    if not encrypted:
        return {}
    try:
        result: dict[str, Any] = json.loads(decrypt(encrypted))
        return result
    except Exception:
        return {}
