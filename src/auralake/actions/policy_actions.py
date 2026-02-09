"""Policy enforcement actions."""
from __future__ import annotations

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class SetAutotermination(AbstractAction):
    name = "set_autotermination"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        minutes = recommendation.recommended_state.get("autotermination_minutes", 60)
        compute.resize(recommendation.resource_id, {"autotermination_minutes": minutes})
        logger.info("autotermination_set", cluster_id=recommendation.resource_id, minutes=minutes)

    def rollback(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        compute.resize(recommendation.resource_id, recommendation.current_state)
