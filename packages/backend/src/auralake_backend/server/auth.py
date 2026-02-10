"""API key authentication."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Generator
from datetime import UTC, datetime

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session, select

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import ApiKey

# ---------------------------------------------------------------------------
# DB session dependency (local to auth to avoid circular imports)
# ---------------------------------------------------------------------------


def _get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:  # type: ignore[no-untyped-call]
        yield session


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def create_api_key(session: Session, name: str) -> tuple[ApiKey, str]:
    """Create an API key. Returns (db_record, raw_key). Raw key is shown once."""
    raw_key = "al_" + secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    record = ApiKey(
        name=name,
        key_hash=key_hash,
        key_prefix=raw_key[:8],
    )
    session.add(record)
    session.commit()
    session.refresh(record)
    return record, raw_key


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

_bearer_scheme = HTTPBearer()


async def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    session: Session = Depends(_get_session),
) -> ApiKey:
    """Verify Authorization: Bearer <key> header."""
    token = credentials.credentials

    key_hash = hashlib.sha256(token.encode()).hexdigest()
    stmt = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)  # noqa: E712
    db_key = session.exec(stmt).first()
    if not db_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    db_key.last_used_at = datetime.now(UTC)
    session.add(db_key)
    session.commit()
    return db_key
