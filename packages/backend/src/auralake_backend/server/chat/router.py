"""Chat API endpoint."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import ApiKey
from auralake_backend.server.auth import require_auth
from auralake_backend.server.chat.schemas import ChatRequest, ChatResponse
from auralake_backend.server.chat.service import ChatService

router = APIRouter()


def _get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


@router.post("/chat")
async def chat(
    request: Request,
    body: ChatRequest,
    _auth: ApiKey = Depends(require_auth),
    session: Session = Depends(_get_session),
) -> ChatResponse:
    """Chat with the Auralake AI assistant about your lakehouse data."""
    try:
        service = ChatService(session, request.app.state.config)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return service.handle(body.message, body.history)
