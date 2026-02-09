"""Tag enforcement actions."""
from __future__ import annotations

from auralake.actions.base import AbstractAction, RiskLevel
from auralake.core.logging import get_logger
from auralake.models.recommendations import Recommendation

logger = get_logger(__name__)


class EnforceTagsAction(AbstractAction):
    name = "enforce_tags"
    risk_level = RiskLevel.LOW

    def execute(self, recommendation: Recommendation) -> None:
        logger.info("tag_enforcement", resource_id=recommendation.resource_id, missing_tags=recommendation.recommended_state.get("missing_tags"))

    def rollback(self, recommendation: Recommendation) -> None:
        logger.warning("tag_rollback_not_applicable")
