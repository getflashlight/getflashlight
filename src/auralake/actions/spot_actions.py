"""Spot instance optimization actions."""
from __future__ import annotations

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class EnableSpotAction(AbstractAction):
    name = "enable_spot"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        compute.resize(recommendation.resource_id, recommendation.recommended_state)
        logger.info("spot_enabled", cluster_id=recommendation.resource_id)

    def rollback(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        compute.resize(recommendation.resource_id, recommendation.current_state)
