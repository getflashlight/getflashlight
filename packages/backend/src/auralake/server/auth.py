"""Optional API-key auth. If AURALAKE_API_KEY is unset, the API is open (local)."""

from __future__ import annotations

from fastapi import Header, HTTPException, status

from auralake.core.settings import get_settings


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """FastAPI dependency enforcing the configured API key when one is set."""
    configured = get_settings().api_key
    if configured and x_api_key != configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing API key"
        )
