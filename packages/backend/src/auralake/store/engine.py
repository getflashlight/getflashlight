"""Database engine and session management (SQLModel / SQLAlchemy)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from auralake.core.exceptions import DatabaseError
from auralake.core.settings import get_settings

_engine: Engine | None = None


def init_engine(database_url: str | None = None) -> None:
    """Initialize the global engine. Defaults to the configured DATABASE_URL."""
    global _engine
    url = database_url or get_settings().database_url
    try:
        _engine = create_engine(url, echo=False, pool_pre_ping=True)
    except Exception as exc:  # noqa: BLE001 - surfaced as DatabaseError
        raise DatabaseError(f"Failed to create database engine: {exc}") from exc


def get_engine() -> Engine:
    """Return the global engine, initializing lazily from settings if needed."""
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context — commits on success, rolls back on error."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
