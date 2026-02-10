"""API key authentication and Better Auth session verification."""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
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
    """Verify Authorization: Bearer <key> header.

    Accepts API keys (prefixed ``al_``) or Better Auth session tokens.
    """
    token = credentials.credentials

    if token.startswith("al_"):
        # API key path
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        stmt = select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active == True)  # noqa: E712
        db_key = session.exec(stmt).first()
        if not db_key:
            raise HTTPException(status_code=401, detail="Invalid API key")
        db_key.last_used_at = datetime.now(UTC)
        session.add(db_key)
        session.commit()
        return db_key

    # Better Auth session token path — verify against session table
    try:
        result: Any = session.execute(
            text('SELECT "userId" FROM session WHERE token = :token'),
            {"token": token},
        ).first()
    except Exception:
        # session table may not exist if Better Auth isn't set up
        result = None
    if result:
        # Return a synthetic ApiKey for the dependency contract
        return ApiKey(name=f"user:{result[0]}", key_hash="", key_prefix="ba_", is_active=True)

    raise HTTPException(status_code=401, detail="Missing or invalid authorization")
