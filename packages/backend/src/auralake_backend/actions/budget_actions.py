"""Budget management actions."""

from __future__ import annotations

from auralake_shared.core.logging import get_logger
from auralake_shared.models.recommendations import ActionResult, Recommendation

from auralake_backend.actions.base import AbstractAction, RiskLevel

logger = get_logger(__name__)


class CreateBudgetAlert(AbstractAction):
    name = "create_budget_alert"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> ActionResult:
        logger.info("budget_alert_created", details=recommendation.recommended_state)
        return ActionResult(
            action_type=self.name,
            resource_id=recommendation.resource_id,
            resource_name=recommendation.resource_name,
            status="applied",
            detail="Budget alert created",
        )

    def rollback(self, recommendation: Recommendation) -> None:
        logger.info("budget_alert_removed")
