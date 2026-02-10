"""CRUD repository for provider connections."""

from __future__ import annotations

import uuid

from sqlmodel import Session, select

from auralake_backend.db.models import ProviderConnection


class ProviderConnectionRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, connection_id: uuid.UUID) -> ProviderConnection | None:
        return self._session.get(ProviderConnection, connection_id)

    def list_all(self) -> list[ProviderConnection]:
        return list(self._session.exec(select(ProviderConnection)).all())

    def list_by_provider(self, provider: str) -> list[ProviderConnection]:
        stmt = select(ProviderConnection).where(ProviderConnection.provider == provider)
        return list(self._session.exec(stmt).all())

    def create(self, conn: ProviderConnection) -> ProviderConnection:
        self._session.add(conn)
        self._session.commit()
        self._session.refresh(conn)
        return conn

    def update(self, conn: ProviderConnection) -> ProviderConnection:
        self._session.add(conn)
        self._session.commit()
        self._session.refresh(conn)
        return conn

    def delete(self, connection_id: uuid.UUID) -> bool:
        conn = self.get(connection_id)
        if conn is None:
            return False
        self._session.delete(conn)
        self._session.commit()
        return True
