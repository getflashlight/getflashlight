"""Policy enforcement actions."""

from __future__ import annotations

from auralake_shared.core.logging import get_logger
from auralake_shared.models.recommendations import ActionResult, Recommendation

from auralake_backend.actions.base import AbstractAction, RiskLevel

logger = get_logger(__name__)


class SetAutotermination(AbstractAction):
    name = "set_autotermination"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> ActionResult:
        compute = self.context.provider.get_compute_client()
        minutes = recommendation.recommended_state.get("autotermination_minutes", 60)
        try:
            compute.resize(recommendation.resource_id, {"autotermination_minutes": minutes})
            logger.info(
                "autotermination_set", cluster_id=recommendation.resource_id, minutes=minutes
            )
            return ActionResult(
                action_type=self.name,
                resource_id=recommendation.resource_id,
                resource_name=recommendation.resource_name,
                status="applied",
                detail=f"Autotermination set to {minutes} min",
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
        compute = self.context.provider.get_compute_client()
        compute.resize(recommendation.resource_id, recommendation.current_state)
