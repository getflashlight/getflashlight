"""Delta Lake maintenance actions."""

from __future__ import annotations

from auralake_shared.core.logging import get_logger
from auralake_shared.models.recommendations import ActionResult, Recommendation

from auralake_backend.actions.base import AbstractAction, RiskLevel

logger = get_logger(__name__)


class OptimizeTableAction(AbstractAction):
    name = "optimize_table"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> ActionResult:
        storage = self.context.provider.get_storage_client()
        try:
            storage.optimize_table(recommendation.resource_id)
            logger.info("table_optimized", table=recommendation.resource_id)
            return ActionResult(
                action_type=self.name,
                resource_id=recommendation.resource_id,
                resource_name=recommendation.resource_name,
                status="applied",
            )
        except Exception as exc:
            return ActionResult(
                action_type=self.name,
                resource_id=recommendation.resource_id,
                resource_name=recommendation.resource_name,
                status="failed",
                error=str(exc),
            )

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("optimize_not_reversible")


class VacuumTableAction(AbstractAction):
    name = "vacuum_table"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> ActionResult:
        storage = self.context.provider.get_storage_client()
        retention = recommendation.recommended_state.get("retention_hours", 168)
        try:
            storage.vacuum_table(recommendation.resource_id, retention)
            logger.info("table_vacuumed", table=recommendation.resource_id)
            return ActionResult(
                action_type=self.name,
                resource_id=recommendation.resource_id,
                resource_name=recommendation.resource_name,
                status="applied",
                detail=f"Vacuumed with {retention}h retention",
            )
        except Exception as exc:
            return ActionResult(
                action_type=self.name,
                resource_id=recommendation.resource_id,
                resource_name=recommendation.resource_name,
                status="failed",
                error=str(exc),
            )

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("vacuum_not_reversible")
