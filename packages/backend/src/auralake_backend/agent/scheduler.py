"""Collection scheduler for the agent loop."""

from __future__ import annotations

import time

from auralake_shared.core.logging import get_logger

logger = get_logger(__name__)


class Scheduler:
    """Simple interval-based scheduler."""

    def __init__(self, interval_seconds: int = 300) -> None:
        self.interval_seconds = interval_seconds
        self._running = False

    def start(self, callback) -> None:
        """Run the callback on a fixed interval."""
        self._running = True
        logger.info("scheduler_started", interval=self.interval_seconds)
        while self._running:
            try:
                callback()
            except Exception as exc:
                logger.error("scheduler_callback_error", error=str(exc))
            time.sleep(self.interval_seconds)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        logger.info("scheduler_stopped")
