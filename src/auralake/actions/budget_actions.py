"""Budget management actions."""
from __future__ import annotations

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class CreateBudgetAlert(AbstractAction):
    name = "create_budget_alert"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> None:
        logger.info("budget_alert_created", details=recommendation.recommended_state)

    def rollback(self, recommendation: Recommendation) -> None:
        logger.info("budget_alert_removed")
