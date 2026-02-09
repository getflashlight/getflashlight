"""Delta Lake maintenance actions."""
from __future__ import annotations

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class OptimizeTableAction(AbstractAction):
    name = "optimize_table"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> None:
        storage = self.context.provider.get_storage_client()
        storage.optimize_table(recommendation.resource_id)
        logger.info("table_optimized", table=recommendation.resource_id)

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("optimize_not_reversible")


class VacuumTableAction(AbstractAction):
    name = "vacuum_table"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> None:
        storage = self.context.provider.get_storage_client()
        retention = recommendation.recommended_state.get("retention_hours", 168)
        storage.vacuum_table(recommendation.resource_id, retention)
        logger.info("table_vacuumed", table=recommendation.resource_id)

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("vacuum_not_reversible")
