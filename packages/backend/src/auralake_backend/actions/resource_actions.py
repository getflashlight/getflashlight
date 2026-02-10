"""Resource cleanup actions."""

from __future__ import annotations

from auralake_shared.core.logging import get_logger
from auralake_shared.models.recommendations import ActionResult, Recommendation

from auralake_backend.actions.base import AbstractAction, RiskLevel

logger = get_logger(__name__)


class TerminateIdleClusterAction(AbstractAction):
    name = "terminate_idle_cluster"
    risk_level = RiskLevel.MEDIUM

    def execute(self, recommendation: Recommendation) -> ActionResult:
        compute = self.context.provider.get_compute_client()
        try:
            compute.terminate(recommendation.resource_id)
            logger.info("idle_cluster_terminated", cluster_id=recommendation.resource_id)
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
        logger.warning("cannot_rollback_terminate", cluster_id=recommendation.resource_id)
