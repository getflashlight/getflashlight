"""Async task manager for collection pipelines.

Manages one collection per connection at a time. Each collection runs
as an asyncio task wrapping a ``FullCollector`` in a thread pool.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from threading import Event
from typing import Any

import structlog
from sqlmodel import Session, select

from auralake_backend.db.engine import get_engine
from auralake_backend.db.models import CollectionRun

logger = structlog.get_logger(__name__)


class CollectionTaskManager:
    """Manages background collection tasks — one per connection."""

    def __init__(self) -> None:
        self._tasks: dict[uuid.UUID, asyncio.Task] = {}
        self._cancel_events: dict[uuid.UUID, Event] = {}

    def start_collection(
        self,
        connection_id: uuid.UUID,
        config: Any,
        trigger: str = "manual",
        mode: str = "incremental",
    ) -> CollectionRun:
        """Launch a background collection for a connection.

        Returns the newly-created ``CollectionRun`` record.
        Raises ``ValueError`` if a collection is already running.
        """
        if connection_id in self._tasks and not self._tasks[connection_id].done():
            raise ValueError(f"Collection already running for connection {connection_id}")

        with Session(get_engine()) as session:
            run = CollectionRun(
                connection_id=connection_id,
                status="running",
                trigger=trigger,
                worker_statuses={},
                started_at=datetime.now(UTC),
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            run_id = run.id

        cancel_event = Event()
        self._cancel_events[connection_id] = cancel_event

        task = asyncio.get_event_loop().create_task(
            self._run_collection(connection_id, run_id, config, cancel_event, mode)
        )
        self._tasks[connection_id] = task
        return run

    async def _run_collection(
        self,
        connection_id: uuid.UUID,
        run_id: uuid.UUID,
        config: Any,
        cancel_event: Event,
        mode: str = "incremental",
    ) -> None:
        """Execute the full collection pipeline in a thread."""
        from auralake_backend.etl.full_collector import FullCollector

        collector = FullCollector(connection_id, config, cancel_event, mode=mode)

        try:
            worker_statuses = await asyncio.to_thread(collector.run, run_id)
            self._finalize_run(run_id, worker_statuses)
        except Exception as exc:
            logger.error(
                "collection_task_failed",
                connection_id=str(connection_id),
                error=str(exc),
            )
            self._fail_run(run_id, str(exc))
        finally:
            self._cancel_events.pop(connection_id, None)

    def retry_worker(
        self,
        connection_id: uuid.UUID,
        worker_name: str,
        config: Any,
    ) -> CollectionRun | None:
        """Retry a single failed worker for a connection.

        Finds the latest CollectionRun and re-runs just the specified worker.
        Returns the updated CollectionRun.
        """
        with Session(get_engine()) as session:
            run = session.exec(
                select(CollectionRun)
                .where(CollectionRun.connection_id == connection_id)
                .order_by(CollectionRun.started_at.desc())  # type: ignore[union-attr]
                .limit(1)
            ).first()
            if not run:
                return None
            run_id = run.id

        cancel_event = Event()
        asyncio.get_event_loop().create_task(
            self._run_retry(connection_id, run_id, worker_name, config, cancel_event)
        )
        return run

    async def _run_retry(
        self,
        connection_id: uuid.UUID,
        run_id: uuid.UUID,
        worker_name: str,
        config: Any,
        cancel_event: Event,
    ) -> None:
        from auralake_backend.etl.full_collector import FullCollector

        collector = FullCollector(connection_id, config, cancel_event)

        try:
            new_statuses = await asyncio.to_thread(collector.run_single_worker, run_id, worker_name)
            # Merge into existing run statuses
            with Session(get_engine()) as session:
                run = session.get(CollectionRun, run_id)
                if run:
                    ws = dict(run.worker_statuses or {})
                    ws.update(new_statuses)
                    run.worker_statuses = ws
                    # Recalculate overall status
                    run.status = _derive_run_status(ws)
                    session.add(run)
                    session.commit()
        except Exception as exc:
            logger.error(
                "retry_worker_failed",
                connection_id=str(connection_id),
                worker_name=worker_name,
                error=str(exc),
            )

    def get_status(self, connection_id: uuid.UUID) -> dict[str, Any] | None:
        """Get the latest collection run status for a connection."""
        with Session(get_engine()) as session:
            run = session.exec(
                select(CollectionRun)
                .where(CollectionRun.connection_id == connection_id)
                .order_by(CollectionRun.started_at.desc())  # type: ignore[union-attr]
                .limit(1)
            ).first()
            if not run:
                return None
            return {
                "id": str(run.id),
                "connection_id": str(run.connection_id),
                "status": run.status,
                "trigger": run.trigger,
                "worker_statuses": run.worker_statuses,
                "error": run.error,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": (run.completed_at.isoformat() if run.completed_at else None),
            }

    def cancel_collection(self, connection_id: uuid.UUID) -> bool:
        """Cancel a running collection."""
        cancel_event = self._cancel_events.get(connection_id)
        if cancel_event:
            cancel_event.set()
            return True
        task = self._tasks.get(connection_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def list_active(self) -> list[dict[str, Any]]:
        """List all currently active collections."""
        active = []
        for conn_id, task in self._tasks.items():
            if not task.done():
                status = self.get_status(conn_id)
                if status:
                    active.append(status)
        return active

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """List past collection runs across all connections."""
        with Session(get_engine()) as session:
            runs = session.exec(
                select(CollectionRun)
                .order_by(CollectionRun.started_at.desc())  # type: ignore[union-attr]
                .limit(limit)
            ).all()
            return [
                {
                    "id": str(r.id),
                    "connection_id": str(r.connection_id),
                    "status": r.status,
                    "trigger": r.trigger,
                    "worker_statuses": r.worker_statuses,
                    "error": r.error,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": (r.completed_at.isoformat() if r.completed_at else None),
                }
                for r in runs
            ]

    def shutdown(self) -> None:
        """Cancel all running tasks on server shutdown."""
        for conn_id in list(self._cancel_events):
            self._cancel_events[conn_id].set()
        for conn_id, task in list(self._tasks.items()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._cancel_events.clear()
        logger.info("task_manager_shutdown")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finalize_run(self, run_id: uuid.UUID, worker_statuses: dict[str, Any]) -> None:
        with Session(get_engine()) as session:
            run = session.get(CollectionRun, run_id)
            if run:
                run.worker_statuses = worker_statuses
                run.status = _derive_run_status(worker_statuses)
                run.completed_at = datetime.now(UTC)
                session.add(run)
                session.commit()

    def _fail_run(self, run_id: uuid.UUID, error: str) -> None:
        with Session(get_engine()) as session:
            run = session.get(CollectionRun, run_id)
            if run:
                run.status = "failed"
                run.error = error
                run.completed_at = datetime.now(UTC)
                session.add(run)
                session.commit()


def _derive_run_status(worker_statuses: dict[str, Any]) -> str:
    """Derive the overall run status from per-worker statuses."""
    statuses = {ws.get("status", "pending") for ws in worker_statuses.values()}
    if "running" in statuses or "pending" in statuses:
        return "running"
    if "failed" in statuses:
        if "completed" in statuses:
            return "completed_with_errors"
        return "failed"
    if "cancelled" in statuses:
        return "cancelled"
    return "completed"
