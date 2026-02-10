"""Exception-to-HTTP-status mapping for the FastAPI server."""

from __future__ import annotations

from auralake_shared.core.exceptions import (
    AuraLakeError,
    AuthenticationError,
    ConfigError,
    DatabaseError,
    DuplicateConnectionError,
    ProviderNotFoundError,
    SafetyError,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

_STATUS_MAP: dict[type[AuraLakeError], int] = {
    ConfigError: 400,
    AuthenticationError: 401,
    SafetyError: 403,
    ProviderNotFoundError: 404,
    DuplicateConnectionError: 409,
    DatabaseError: 503,
}


def register_exception_handlers(app: FastAPI) -> None:
    """Register auralake exception handlers on the given FastAPI application."""

    @app.exception_handler(AuraLakeError)
    async def _handle_auralake_error(
        request: Request,
        exc: AuraLakeError,
    ) -> JSONResponse:
        status_code = 500
        for exc_type, code in _STATUS_MAP.items():
            if isinstance(exc, exc_type):
                status_code = code
                break

        return JSONResponse(
            status_code=status_code,
            content={
                "error": type(exc).__name__,
                "detail": str(exc),
            },
        )
