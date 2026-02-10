"""Database engine and session management using SQLModel."""

from __future__ import annotations

from auralake_shared.core.exceptions import DatabaseError
from sqlmodel import Session, SQLModel, create_engine

_engine = None


def init_engine(database_url: str) -> None:
    """Initialize the global database engine."""
    global _engine
    try:
        _engine = create_engine(database_url, echo=False)
    except Exception as exc:
        raise DatabaseError(f"Failed to create database engine: {exc}") from exc


def get_engine():
    """Return the global database engine, raising if not initialized."""
    if _engine is None:
        raise DatabaseError("Database engine not initialized. Run `auralake db init` first.")
    return _engine


def get_session() -> Session:
    """Create a new database session."""
    return Session(get_engine())


def create_all_tables() -> None:
    """Create all SQLModel tables."""
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
