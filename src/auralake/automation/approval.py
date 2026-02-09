"""Approval logic for automation actions."""
from __future__ import annotations

from auralake.core.context import ExecutionContext
from auralake.core.output import confirm_action
from auralake.models.config import AutomationLevel
from auralake.models.recommendations import Recommendation, RiskLevel


def needs_approval(context: ExecutionContext, recommendation: Recommendation) -> bool:
    """Determine if a recommendation needs explicit user approval."""
    level = context.automation_level

    if level in (AutomationLevel.RECOMMEND, AutomationLevel.DRY_RUN):
        return False  # No actions taken

    if level == AutomationLevel.APPLY:
        return True  # Always confirm in apply mode

    if level == AutomationLevel.AUTO:
        # Auto mode: only approve high/critical risk
        return recommendation.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)

    return True


def request_approval(recommendation: Recommendation) -> bool:
    """Ask user for approval of a specific action."""
    return confirm_action(
        f"[{recommendation.risk_level.upper()}] {recommendation.title} "
        f"(saves ~${recommendation.estimated_monthly_savings_usd}/mo)?"
    )
