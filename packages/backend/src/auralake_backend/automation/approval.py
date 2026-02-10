"""Approval logic for automation actions."""

from __future__ import annotations

from typing import Protocol

from auralake_shared.core.context import ExecutionContext
from auralake_shared.models.config import AutomationLevel
from auralake_shared.models.recommendations import Recommendation, RiskLevel


class ApprovalStrategy(Protocol):
    """Protocol for approval strategies used by the automation engine."""

    def should_approve(self, recommendation: Recommendation) -> bool:
        """Return True if the action should proceed."""
        ...


class AutoApproval:
    """Always approves — used by server/API (no interactive prompts)."""

    def should_approve(self, recommendation: Recommendation) -> bool:
        return True


class DenyApproval:
    """Always denies — used for dry-run or recommend-only modes."""

    def should_approve(self, recommendation: Recommendation) -> bool:
        return False


class InteractiveApproval:
    """Prompts user via Rich console — used by CLI only."""

    def should_approve(self, recommendation: Recommendation) -> bool:
        from auralake_shared.core.output import confirm_action

        return confirm_action(
            f"[{recommendation.risk_level.upper()}] {recommendation.title} "
            f"(saves ~${recommendation.estimated_monthly_savings_usd}/mo)?"
        )


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
