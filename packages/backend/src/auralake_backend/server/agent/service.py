"""Agent service — delegates to CollectionTaskManager."""

from __future__ import annotations

import uuid
from typing import Any

from auralake_backend.server.agent.task_manager import CollectionTaskManager


class AgentService:
    """Thin wrapper around the task manager for use by settings router."""

    def __init__(self, task_manager: CollectionTaskManager) -> None:
        self._tm = task_manager

    def trigger_collection(
        self, connection_id: uuid.UUID, config: Any, trigger: str = "auto"
    ) -> None:
        """Start a collection if one is not already running."""
        try:
            self._tm.start_collection(connection_id, config, trigger=trigger)
        except ValueError:
            pass  # Already running — skip
