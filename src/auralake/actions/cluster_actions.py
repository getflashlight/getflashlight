"""Cluster optimization actions."""
from __future__ import annotations

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class ResizeClusterAction(AbstractAction):
    name = "resize_cluster"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        new_config = recommendation.recommended_state
        logger.info("resize_cluster", cluster_id=recommendation.resource_id, new_config=new_config)
        compute.resize(recommendation.resource_id, new_config)

    def rollback(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        old_config = recommendation.current_state
        logger.info("rollback_resize", cluster_id=recommendation.resource_id)
        compute.resize(recommendation.resource_id, old_config)


class TerminateClusterAction(AbstractAction):
    name = "terminate_cluster"
    risk_level = RiskLevel.HIGH

    def execute(self, recommendation: Recommendation) -> None:
        compute = self.context.provider.get_compute_client()
        logger.info("terminate_cluster", cluster_id=recommendation.resource_id)
        compute.terminate(recommendation.resource_id)

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("cannot_rollback_terminate", cluster_id=recommendation.resource_id)

    def validate(self, recommendation: Recommendation) -> bool:
        protected = self.context.config.automation.protected_clusters
        if recommendation.resource_id in protected:
            logger.warning("protected_cluster", cluster_id=recommendation.resource_id)
            return False
        return True
