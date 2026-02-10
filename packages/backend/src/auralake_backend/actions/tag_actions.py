"""Tag enforcement actions."""

from __future__ import annotations

from auralake_shared.core.logging import get_logger
from auralake_shared.models.recommendations import ActionResult, Recommendation

from auralake_backend.actions.base import AbstractAction, RiskLevel

logger = get_logger(__name__)


class EnforceTagsAction(AbstractAction):
    name = "enforce_tags"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> ActionResult:
        logger.info(
            "tag_enforcement",
            resource_id=recommendation.resource_id,
            missing_tags=recommendation.recommended_state.get("missing_tags"),
        )
        return ActionResult(
            action_type=self.name,
            resource_id=recommendation.resource_id,
            resource_name=recommendation.resource_name,
            status="applied",
            detail="Tags enforced",
        )

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("tag_rollback_not_applicable")
