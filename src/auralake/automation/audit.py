"""Audit trail for all automation actions."""
from __future__ import annotations

from auralake.core.context import ExecutionContext
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class AuditTrail:
    """Records all actions taken or attempted."""

    def __init__(self, context: ExecutionContext) -> None:
        self.context = context
        self._entries: list[dict] = []

    def record(
        self,
        recommendation: Recommendation,
        status: str,
        detail: str | None = None,
    ) -> None:
        """Record an action in the audit trail."""
        entry = {
            "action_type": recommendation.type,
            "resource_id": recommendation.resource_id,
            "resource_name": recommendation.resource_name,
            "workspace_id": recommendation.workspace_id,
            "provider": self.context.config.provider,
            "automation_level": self.context.automation_level.value,
            "status": status,
            "detail": detail,
            "title": recommendation.title,
        }
        self._entries.append(entry)
        logger.info("audit_entry", **entry)

        # Try to persist to database
        try:
            self._persist(entry, recommendation)
        except Exception:
            logger.warning("audit_persist_failed", entry=entry)

    def _persist(self, entry: dict, recommendation: Recommendation) -> None:
        """Persist audit entry to the database."""
        from auralake.db.engine import get_session
        from auralake.db.models import AuditLog
        from datetime import datetime

        with get_session() as session:
            log = AuditLog(
                action_type=entry["action_type"],
                resource_id=entry["resource_id"],
                workspace_id=entry.get("workspace_id"),
                provider=entry["provider"],
                automation_level=entry["automation_level"],
                before_state=recommendation.current_state,
                after_state=recommendation.recommended_state,
                status=entry["status"],
                error_message=entry.get("detail"),
                executed_at=datetime.utcnow(),
            )
            session.add(log)
            session.commit()

    @property
    def entries(self) -> list[dict]:
        return list(self._entries)
