"""Bearer-token authentication for the FastAPI server.

If the environment variable referenced by ``config.server.api_key_env`` (default
``AURALAKE_API_KEY``) is **not set**, authentication is disabled and all
requests are allowed through.
"""

from __future__ import annotations

import os

from auralake_shared.models.config import AuraLakeConfig
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_expected_key(request: Request) -> str | None:
    """Read the expected API key from the env var specified in config.

    Returns ``None`` when the env var is unset (auth disabled).
    """
    config: AuraLakeConfig = request.app.state.config
    env_var = config.server.api_key_env
    return os.environ.get(env_var)


def require_auth(
    expected_key: str | None = Depends(_get_expected_key),
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
) -> None:
    """FastAPI dependency that enforces bearer-token authentication.

    When the expected key env var is not set, all requests are permitted.
    Otherwise the ``Authorization: Bearer <token>`` header must carry the
    matching token.
    """
    if expected_key is None:
        # Auth disabled — env var not configured.
        return

    if credentials is None or credentials.credentials != expected_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
