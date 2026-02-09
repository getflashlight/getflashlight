"""Resource cleanup actions."""
from __future__ import annotations

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class TerminateIdleClusterAction(AbstractAction):
    name = "terminate_idle_cluster"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        compute.terminate(recommendation.resource_id)
        logger.info("idle_cluster_terminated", cluster_id=recommendation.resource_id)

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("cannot_rollback_terminate", cluster_id=recommendation.resource_id)
