"""Progressive automation engine.

Handles the execution lifecycle: recommend -> dry-run -> apply -> auto -> PR.
"""

from __future__ import annotations

from auralake_shared.core.context import ExecutionContext
from auralake_shared.core.exceptions import SafetyError
from auralake_shared.core.logging import get_logger
from auralake_shared.models.config import AutomationLevel
from auralake_shared.models.recommendations import ActionResult, RiskLevel

from auralake_backend.automation.approval import ApprovalStrategy, AutoApproval
from auralake_backend.automation.audit import AuditTrail
from auralake_backend.automation.plan import ExecutionPlan

logger = get_logger(__name__)


class AutomationEngine:
    """Orchestrates action execution based on automation level."""

    def __init__(
        self,
        context: ExecutionContext,
        approval: ApprovalStrategy | None = None,
    ) -> None:
        self.context = context
        self.audit = AuditTrail(context)
        self.approval = approval or AutoApproval()

    def execute(self, plan: ExecutionPlan) -> list[ActionResult]:
        """Execute an action plan according to the automation level."""
        level = self.context.automation_level
        results: list[ActionResult] = []

        for step in plan.steps:
            rec = step.recommendation
            action_fn = step.action_fn

            if level == AutomationLevel.RECOMMEND:
                results.append(
                    ActionResult(
                        action_type=rec.type,
                        resource_id=rec.resource_id,
                        resource_name=rec.resource_name,
                        status="recommended",
                        detail=rec.title,
                    )
                )
                continue

            if level == AutomationLevel.DRY_RUN:
                results.append(
                    ActionResult(
                        action_type=rec.type,
                        resource_id=rec.resource_id,
                        resource_name=rec.resource_name,
                        status="dry_run",
                        detail=str(rec.recommended_state),
                    )
                )
                continue

            if level == AutomationLevel.APPLY:
                if not self.approval.should_approve(rec):
                    self.audit.record(rec, "skipped", "User declined")
                    results.append(
                        ActionResult(
                            action_type=rec.type,
                            resource_id=rec.resource_id,
                            resource_name=rec.resource_name,
                            status="skipped",
                            detail="User declined",
                        )
                    )
                    continue

            if level == AutomationLevel.AUTO:
                try:
                    self._check_safety(rec)
                except SafetyError as exc:
                    self.audit.record(rec, "skipped", str(exc))
                    results.append(
                        ActionResult(
                            action_type=rec.type,
                            resource_id=rec.resource_id,
                            resource_name=rec.resource_name,
                            status="skipped",
                            detail=str(exc),
                        )
                    )
                    continue

            # Execute the action
            self.audit.record(rec, "started")
            try:
                action_fn()
                self.audit.record(rec, "completed")
                results.append(
                    ActionResult(
                        action_type=rec.type,
                        resource_id=rec.resource_id,
                        resource_name=rec.resource_name,
                        status="applied",
                        detail=rec.title,
                    )
                )
                logger.info("action_applied", title=rec.title)
            except Exception as exc:
                self.audit.record(rec, "failed", str(exc))
                results.append(
                    ActionResult(
                        action_type=rec.type,
                        resource_id=rec.resource_id,
                        resource_name=rec.resource_name,
                        status="failed",
                        error=str(exc),
                    )
                )
                logger.warning("action_failed", title=rec.title, error=str(exc))

        return results

    def _check_safety(self, rec) -> None:
        """Enforce safety rails for auto mode."""
        max_risk = self.context.config.automation.max_auto_risk_level
        risk_order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        max_idx = next((i for i, r in enumerate(risk_order) if r.value == max_risk), 1)
        rec_idx = next((i for i, r in enumerate(risk_order) if r == rec.risk_level), 3)
        if rec_idx > max_idx:
            raise SafetyError(
                f"Action '{rec.title}' has risk level {rec.risk_level} which exceeds "
                f"max auto risk level '{max_risk}'"
            )

        # Check protected resources
        protected = set(
            self.context.config.automation.protected_clusters
            + self.context.config.automation.protected_jobs
        )
        if rec.resource_id in protected:
            raise SafetyError(f"Resource '{rec.resource_id}' is protected from auto actions")
